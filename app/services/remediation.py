import asyncio
import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from ..agent import analyze_incident
from ..config import get_settings
from ..database import (
    create_deployment,
    get_incident,
    get_incident_by_deployment,
    list_deployments,
    list_incidents,
    update_incident,
)
from .integrations import (
    github_mcp_service,
    render_service,
    runtime_connection_service,
    vercel_service,
)
from .llm import llm_service
from .memory import memory_service


SOURCE_EXTENSIONS = {
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".py",
    ".go",
    ".java",
    ".rb",
    ".php",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
}

PATH_PATTERN = re.compile(
    r"(?P<path>(?:\.?/)?[A-Za-z0-9_./@()-]+"
    r"\.(?:js|jsx|ts|tsx|mjs|cjs|py|go|java|rb|php|json|yaml|yml|toml))"
    r"(?::(?P<line>\d+)(?::\d+)?)?"
)


class RemediationService:
    @staticmethod
    def _error_text(error: BaseException) -> str:
        nested = getattr(error, "exceptions", None)
        if nested:
            details = [
                RemediationService._error_text(item)
                for item in nested
            ]
            return "; ".join(detail for detail in details if detail)
        return str(error)

    @staticmethod
    def _retain(callback: Any, value: dict[str, Any]) -> None:
        try:
            callback(value)
        except Exception:
            # Cloud memory must not block incident remediation or draft PR creation.
            pass

    async def sync_failed_deployments(
        self,
        authorization: str,
        user_id: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        runtime = await runtime_connection_service.get(authorization)
        if not runtime.get("vercelToken") or not runtime.get("projects"):
            raise RuntimeError("Connect and map at least one Vercel project")
        results: list[dict[str, Any]] = []
        failure_count = 0
        processed_deployments: set[str] = set()

        # Retry incidents already captured in cloud/local storage before calling Vercel.
        # This keeps remediation moving when project listing later loses team access.
        for incident in list_incidents(limit=100, user_id=user_id):
            if incident.get("pull_request_url") or not incident.get("deployment_id"):
                continue
            processed_deployments.add(str(incident["deployment_id"]))
            results.append(await self._retry_incident(incident, runtime))

        # A deployment may already be captured even if Vercel later rejects list
        # access. Build the incident from the stored payload instead.
        for stored in list_deployments(limit=100, user_id=user_id):
            deployment_id = str(stored["id"])
            if (
                deployment_id in processed_deployments
                or get_incident_by_deployment(deployment_id, user_id)
                or stored.get("status") not in {"ERROR", "CANCELED"}
            ):
                continue
            processed_deployments.add(deployment_id)
            try:
                results.append(await self._analyze_stored_deployment(stored, runtime, user_id))
            except Exception as error:
                results.append({
                    "deployment_id": deployment_id,
                    "error": self._error_text(error),
                })

        for project in runtime["projects"]:
            try:
                failures = await vercel_service.failed_deployments(
                    runtime["vercelToken"], project["vercel_project_id"],
                    project.get("vercel_team_id"), limit,
                )
            except Exception as error:
                results.append({
                    "project_id": project["vercel_project_id"],
                    "repository": project.get("github_repository"),
                    "error": str(error),
                })
                continue
            failure_count += len(failures)
            for summary in failures:
                deployment_id = summary.get("uid") or summary.get("id")
                if not deployment_id or deployment_id in processed_deployments:
                    continue
                existing = get_incident_by_deployment(deployment_id, user_id)
                if existing:
                    results.append(await self._retry_incident(existing, runtime))
                    continue
                try:
                    results.append(await self._analyze_deployment(deployment_id, project, runtime, user_id))
                except Exception as error:
                    results.append({"deployment_id": deployment_id, "error": str(error)})
        return {
            "tracked_projects": len(runtime["projects"]),
            "failed_deployments": failure_count,
            "results": results,
        }

    async def sync_failed_render_deployments(
        self,
        authorization: str,
        user_id: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        runtime = await runtime_connection_service.get(authorization)
        if not runtime.get("renderToken") or not runtime.get("renderServices"):
            raise RuntimeError("Connect Render and import a matching repository")
        results: list[dict[str, Any]] = []
        failure_count = 0
        for service in runtime["renderServices"]:
            try:
                failures = await render_service.failed_deployments(
                    runtime["renderToken"],
                    service["render_service_id"],
                    limit,
                )
            except Exception as error:
                results.append({
                    "service_id": service["render_service_id"],
                    "repository": service.get("github_repository"),
                    "error": str(error),
                })
                continue
            failure_count += len(failures)
            for deploy in failures:
                deployment_id = str(deploy.get("id") or "")
                if not deployment_id:
                    continue
                existing = get_incident_by_deployment(deployment_id, user_id)
                if existing:
                    results.append(await self._retry_incident(existing, runtime))
                    continue
                try:
                    logs = await render_service.logs(
                        runtime["renderToken"],
                        service.get("render_owner_id") or "",
                        service["render_service_id"],
                        deploy,
                    )
                    git = render_service.git_metadata(deploy)
                    repository = service["github_repository"]
                    stored = create_deployment({
                        "id": deployment_id,
                        "user_id": user_id,
                        "platform": "render",
                        "service": service["render_service_name"],
                        "status": deploy.get("status") or "build_failed",
                        "commit_sha": git["commit_sha"],
                        "message": git["commit_message"] or "Render deployment failed",
                        "created_at": deploy.get("createdAt"),
                        "raw_payload": {
                            "deployment": {
                                **deploy,
                                "name": service["render_service_name"],
                                "readyState": deploy.get("status"),
                                "meta": {
                                    "githubOrg": repository.split("/", 1)[0],
                                    "githubRepo": repository.split("/", 1)[1],
                                    "githubCommitSha": git["commit_sha"],
                                    "githubCommitMessage": git["commit_message"],
                                    "githubCommitRef": deploy.get("branch") or "main",
                                },
                            },
                            "repository": repository,
                            "git_ref": deploy.get("branch") or "main",
                            "logs": logs,
                        },
                    })
                    self._retain(memory_service.retain_failure, stored)
                    results.append(await self._analyze_stored_deployment(stored, runtime, user_id))
                except Exception as error:
                    results.append({
                        "deployment_id": deployment_id,
                        "error": self._error_text(error),
                    })
        return {
            "tracked_services": len(runtime["renderServices"]),
            "failed_deployments": failure_count,
            "results": results,
        }

    async def _retry_incident(
        self,
        incident: dict[str, Any],
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        deployment_id = str(incident["deployment_id"])
        try:
            incident = await self._ensure_fix(incident, runtime)
            incident = update_incident(incident["id"], {"remediation_error": None}) or incident
            self._retain(memory_service.retain_remediation, incident)
        except Exception as error:
            message = str(error)
            incident = update_incident(incident["id"], {"remediation_error": message}) or incident
            return {
                "deployment_id": deployment_id,
                "incident": incident,
                "existing": True,
                "fix_error": message,
            }
        try:
            incident = await self._create_draft_pr_from_runtime(incident, runtime)
            incident = update_incident(incident["id"], {"remediation_error": None}) or incident
            self._retain(memory_service.retain_remediation, incident)
        except Exception as error:
            message = str(error)
            incident = update_incident(incident["id"], {"remediation_error": message}) or incident
            return {
                "deployment_id": deployment_id,
                "incident": incident,
                "existing": True,
                "pull_request_error": message,
            }
        return {"deployment_id": deployment_id, "incident": incident, "existing": True}

    async def _ensure_fix(
        self,
        incident: dict[str, Any],
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        if incident.get("fixed_content"):
            return incident
        required = ("repository", "file_path", "commit_sha")
        missing = [field for field in required if not incident.get(field)]
        if missing:
            raise RuntimeError(f"Incident is missing fix context: {', '.join(missing)}")
        source_file = await github_mcp_service.file(
            runtime["githubToken"],
            incident["repository"],
            incident["file_path"],
            incident["commit_sha"],
        )
        source = str(source_file["content"])
        fix = llm_service.fix_suggestion(incident, source, incident["file_path"])
        updated = update_incident(
            incident["id"],
            {
                "fix_summary": fix["summary"],
                "fix_rationale": fix["rationale"],
                "fix_diff": fix["diff"],
                "fixed_content": fix["fixed_content"],
            },
        )
        if not updated:
            raise RuntimeError("Generated fix could not be persisted")
        return updated

    async def _analyze_deployment(
        self,
        deployment_id: str,
        project: dict[str, Any],
        runtime: dict[str, Any],
        user_id: str,
    ) -> dict[str, Any]:
        deployment = await vercel_service.deployment(runtime["vercelToken"], project.get("vercel_team_id"), deployment_id)
        events = await vercel_service.events(runtime["vercelToken"], project.get("vercel_team_id"), deployment_id)
        logs = vercel_service.log_text(events)
        git = vercel_service.git_metadata(deployment)
        repository = git["repository"] or project["github_repository"]
        stored_deployment = create_deployment(
            {
                "id": deployment_id,
                "user_id": user_id,
                "platform": "vercel",
                "service": deployment.get("name") or project["vercel_project_name"],
                "status": deployment.get("readyState")
                or deployment.get("status")
                or "ERROR",
                "commit_sha": git["commit_sha"],
                "message": deployment.get("errorMessage")
                or git.get("commit_message")
                or "Vercel deployment failed",
                "created_at": self._timestamp(deployment.get("createdAt")),
                "raw_payload": {
                    "deployment": deployment,
                    "repository": repository,
                    "git_ref": git["git_ref"],
                    "logs": logs,
                },
            }
        )
        self._retain(memory_service.retain_failure, stored_deployment)
        return await self._analyze_stored_deployment(stored_deployment, runtime, user_id)

    async def _analyze_stored_deployment(
        self,
        stored_deployment: dict[str, Any],
        runtime: dict[str, Any],
        user_id: str,
    ) -> dict[str, Any]:
        deployment_id = str(stored_deployment["id"])
        platform = str(stored_deployment.get("platform") or "vercel")
        provider_name = platform.title()
        raw_payload = stored_deployment.get("raw_payload") or {}
        deployment = raw_payload.get("deployment") or {}
        logs = str(raw_payload.get("logs") or "")
        git = vercel_service.git_metadata(deployment)
        repository = raw_payload.get("repository") or git["repository"]
        if not git["commit_sha"]:
            raise RuntimeError("Vercel deployment did not include a Git commit SHA")
        if not repository:
            raise RuntimeError("Vercel deployment did not include a GitHub repository")

        commit = await github_mcp_service.commit(runtime["githubToken"], repository, str(git["commit_sha"]))
        changed_files = self._changed_files(commit)
        commit_diff = self._commit_diff(commit)
        if commit_diff:
            logs = f"{logs}\n\nGit diff against parent commit:\n{commit_diff[-20_000:]}"
        file_path, line_number = self._select_file(logs, changed_files)
        if not file_path:
            raise RuntimeError(
                "Could not identify the failing source file from Vercel logs or commit"
            )
        source_file = await github_mcp_service.file(
            runtime["githubToken"], repository, file_path, str(git["commit_sha"])
        )
        source = str(source_file["content"])
        code_snippet = self._snippet(source, line_number)

        raw_payload["file_path"] = file_path
        incident = await asyncio.to_thread(
            analyze_incident,
            {
                "title": f"{stored_deployment['service']} deployment failed",
                "user_id": user_id,
                "description": deployment.get("errorMessage")
                or deployment.get("errorCode")
                or f"{provider_name} reported a failed production deployment",
                "error_logs": logs,
                "service_hint": stored_deployment["service"],
                "source": platform,
                "deployment_id": deployment_id,
                "commit_sha": git["commit_sha"],
                "repository": repository,
                "git_ref": git["git_ref"],
                "file_path": file_path,
                "code_snippet": code_snippet,
            },
        )
        try:
            fix = llm_service.fix_suggestion(incident, source, file_path)
        except Exception as error:
            message = str(error)
            incident = update_incident(
                incident["id"],
                {"remediation_error": message},
            ) or incident
            return {
                "deployment_id": deployment_id,
                "incident": incident,
                "existing": False,
                "fix_error": message,
            }
        incident = update_incident(
            incident["id"],
            {
                "file_path": file_path,
                "code_snippet": code_snippet,
                "fix_summary": fix["summary"],
                "fix_rationale": fix["rationale"],
                "fix_diff": fix["diff"],
                "fixed_content": fix["fixed_content"],
                "remediation_error": None,
            },
        )
        if not incident:
            raise RuntimeError("Incident fix could not be persisted")
        self._retain(memory_service.retain_remediation, incident)
        pull_request_error = None
        try:
            incident = await self._create_draft_pr_from_runtime(incident, runtime)
            self._retain(memory_service.retain_remediation, incident)
        except Exception as error:
            pull_request_error = str(error)
        return {
            "deployment_id": deployment_id,
            "incident": incident,
            "existing": False,
            "pull_request_error": pull_request_error,
        }

    async def create_draft_pr(
        self,
        incident_id: str,
        authorization: str,
        user_id: str,
    ) -> dict[str, Any]:
        runtime = await runtime_connection_service.get(authorization)
        incident = get_incident(incident_id, user_id)
        if not incident:
            raise ValueError("Incident not found")
        if incident.get("pull_request_url"):
            return {
                "incident": incident,
                "pull_request_url": incident["pull_request_url"],
            }
        updated = await self._create_draft_pr_from_runtime(incident, runtime)
        self._retain(memory_service.retain_remediation, updated)
        return {"incident": updated, "pull_request_url": updated["pull_request_url"]}

    async def _create_draft_pr_from_runtime(
        self,
        incident: dict[str, Any],
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        if incident.get("pull_request_url"):
            return incident
        required = ("repository", "file_path", "commit_sha", "fixed_content")
        missing = [field for field in required if not incident.get(field)]
        if missing:
            raise RuntimeError(f"Incident is missing remediation data: {', '.join(missing)}")
        source_file = await github_mcp_service.file(
            runtime["githubToken"], incident["repository"], incident["file_path"], incident["commit_sha"]
        )
        branch_name = f"recallops/fix-{incident['id'][:8]}"
        base_branch = incident.get("git_ref") or get_settings().github_default_branch
        url = await github_mcp_service.create_draft_pull_request(
            token=runtime["githubToken"],
            repository=incident["repository"],
            base_branch=base_branch,
            file_path=incident["file_path"],
            current_file_sha=str(source_file.get("sha") or ""),
            fixed_content=incident["fixed_content"],
            title=f"fix: {incident['fix_summary'][:72]}",
            body=(
                "Draft remediation generated from a failed Vercel deployment.\n\n"
                f"Deployment: {incident['deployment_id']}\n"
                f"Commit: {incident['commit_sha']}\n\n"
                f"Diagnosis: {incident['diagnosis']}\n\n"
                f"Rationale: {incident['fix_rationale']}\n\n"
                "This pull request requires human review and validation."
            ),
            branch_name=branch_name,
        )
        updated = update_incident(incident["id"], {"pull_request_url": url})
        if not updated:
            raise RuntimeError("Draft pull request was created but the incident could not be updated")
        return updated

    @staticmethod
    def _changed_files(commit: Any) -> list[str]:
        files: list[str] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                filename = value.get("filename") or value.get("path")
                if (
                    isinstance(filename, str)
                    and PurePosixPath(filename).suffix in SOURCE_EXTENSIONS
                ):
                    files.append(filename.lstrip("./"))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(commit)
        return list(dict.fromkeys(files))

    @staticmethod
    def _commit_diff(commit: Any) -> str:
        patches: list[str] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                filename = value.get("filename") or value.get("path")
                patch = value.get("patch")
                if filename and patch:
                    patches.append(f"--- {filename}\n+++ {filename}\n{patch}")
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(commit)
        return "\n".join(dict.fromkeys(patches))

    @staticmethod
    def _select_file(logs: str, changed_files: list[str]) -> tuple[str | None, int | None]:
        dependency_failure = any(
            marker in logs.lower()
            for marker in ("npm error", "eresolve", "npm install", "yarn install", "pnpm install")
        )
        if dependency_failure:
            package_json = next(
                (path for path in changed_files if PurePosixPath(path).name == "package.json"),
                "package.json",
            )
            return package_json, None
        for match in reversed(list(PATH_PATTERN.finditer(logs))):
            candidate = match.group("path").lstrip("./")
            if not changed_files or candidate in changed_files:
                return candidate, int(match.group("line")) if match.group("line") else None
        return (changed_files[0], None) if changed_files else (None, None)

    @staticmethod
    def _snippet(source: str, line_number: int | None, radius: int = 8) -> str:
        lines = source.splitlines()
        if not lines:
            return ""
        center = max(1, min(line_number or 1, len(lines)))
        start = max(1, center - radius)
        end = min(len(lines), center + radius)
        return "\n".join(
            f"{index:>5} | {lines[index - 1]}" for index in range(start, end + 1)
        )

    @staticmethod
    def _timestamp(value: Any) -> str:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, UTC).isoformat()
        return str(value or datetime.now(UTC).isoformat())


remediation_service = RemediationService()
