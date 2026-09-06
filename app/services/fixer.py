"""Clone-based fix agent: clone the repository, generate a multi-file fix with
the LLM, push a branch, and open a draft PR with the user's GitHub token."""

import asyncio
import difflib
import os
import re
import shutil
import stat
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from ..config import get_settings
from ..db.repo import update_incident
from .github_rest import github_rest_service
from .llm import llm_service

SOURCE_EXTENSIONS = {
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".py", ".go", ".java", ".rb", ".php",
    ".json", ".yaml", ".yml", ".toml",
}

PATH_PATTERN = re.compile(
    r"(?P<path>(?:\.?/)?[A-Za-z0-9_./@()-]+"
    r"\.(?:js|jsx|ts|tsx|mjs|cjs|py|go|java|rb|php|json|yaml|yml|toml))"
    r"(?::(?P<line>\d+)(?::\d+)?)?"
)

DEPENDENCY_MARKERS = ("npm error", "eresolve", "npm install", "yarn install", "pnpm install", "pip install", "modulenotfounderror")
LOCKFILE_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock"}
MAX_CANDIDATE_FILES = 6
MAX_FILE_BYTES = 30_000


class FixerError(RuntimeError):
    pass


def _rmtree_force(path: Path) -> None:
    def onexc(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=onexc)


class CloneFixerService:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _git(self, workspace: Path, *args: str, redact: str | None = None) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        raw, _ = await process.communicate()
        output = raw.decode("utf-8", errors="replace")
        if redact:
            output = output.replace(redact, "***")
        if process.returncode != 0:
            command = " ".join("***" if redact and redact in arg else arg for arg in args)
            raise FixerError(f"git {command} failed: {output[-2000:]}")
        return output

    async def fix_and_pr(self, incident: dict[str, Any], github_token: str) -> dict[str, Any]:
        """Returns the updated incident with pull_request_url set."""
        if incident.get("pull_request_url"):
            return incident
        repository = incident.get("repository")
        if not repository:
            raise FixerError("Incident has no linked GitHub repository")
        settings = get_settings()
        branch = incident.get("git_ref") or settings.github_default_branch
        workspace = settings.workspaces_path / incident["id"][:8]

        async with self._locks[repository.lower()]:
            if workspace.exists():
                _rmtree_force(workspace)
            workspace.mkdir(parents=True)
            try:
                return await self._run(incident, github_token, repository, branch, workspace)
            finally:
                try:
                    _rmtree_force(workspace)
                except OSError:
                    pass

    async def _run(
        self,
        incident: dict[str, Any],
        token: str,
        repository: str,
        branch: str,
        workspace: Path,
    ) -> dict[str, Any]:
        clone_url = f"https://x-access-token:{token}@github.com/{repository}.git"
        await self._git(
            workspace, "clone", "--depth", "50", "--branch", branch,
            clone_url, ".", redact=token,
        )

        logs = str(incident.get("error_logs") or "")
        commit_diff = await self._git(workspace, "log", "-1", "-p", "--no-color")
        tree = (await self._git(workspace, "ls-files")).splitlines()
        candidates = self._candidate_files(incident, logs, tree)
        files = []
        for path in candidates:
            file_path = workspace / path
            try:
                content = file_path.read_text(encoding="utf-8")[:MAX_FILE_BYTES]
            except (OSError, UnicodeDecodeError):
                continue
            files.append({"path": path, "content": content})
        if not files:
            raise FixerError("Could not identify candidate source files to fix")

        fix = await asyncio.to_thread(
            llm_service.fix_files, incident, files, tree, logs, commit_diff
        )
        applied = self._apply_fix(workspace, tree, fix, logs)

        branch_name = f"recallops/fix-{incident['id'][:8]}"
        summary = str(fix.get("summary") or "automated remediation")[:72]
        await self._git(workspace, "checkout", "-b", branch_name)
        await self._git(workspace, "add", "-A")
        await self._git(
            workspace,
            "-c", "user.name=RecallOps Agent",
            "-c", "user.email=agent@recallops.local",
            "commit", "-m", f"fix: {summary}",
        )
        await self._git(workspace, "push", "origin", branch_name, redact=token)

        url = await github_rest_service.create_pull_request(
            token,
            repository,
            head=branch_name,
            base=branch,
            title=f"fix: {summary}",
            body=(
                "Draft remediation generated by the RecallOps self-healing agent.\n\n"
                f"Incident: {incident['id']}\n"
                f"Service: {incident.get('service')}\n"
                + (f"Deployment: {incident['deployment_id']}\n" if incident.get("deployment_id") else "")
                + (f"Commit: {incident['commit_sha']}\n" if incident.get("commit_sha") else "")
                + f"\nDiagnosis: {incident.get('diagnosis')}\n\n"
                f"Rationale: {fix.get('rationale')}\n\n"
                "This pull request requires human review and validation."
            ),
            draft=True,
        )
        updated = update_incident(
            incident["id"],
            {
                "fix_summary": str(fix.get("summary") or ""),
                "fix_rationale": str(fix.get("rationale") or ""),
                "fix_diff": applied["diff"],
                "fixed_content": applied["primary_content"],
                "file_path": incident.get("file_path") or applied["primary_path"],
                "pull_request_url": url,
                "remediation_error": None,
            },
        )
        if not updated:
            raise FixerError("Draft pull request was created but the incident could not be updated")
        return updated

    def _candidate_files(
        self, incident: dict[str, Any], logs: str, tree: list[str]
    ) -> list[str]:
        tree_set = set(tree)
        candidates: list[str] = []
        if incident.get("file_path") and incident["file_path"] in tree_set:
            candidates.append(incident["file_path"])
        for match in reversed(list(PATH_PATTERN.finditer(logs))):
            path = match.group("path").lstrip("./")
            if path in tree_set and path not in candidates:
                candidates.append(path)
            # Log paths often carry a build prefix; fall back to suffix match.
            elif path not in candidates:
                suffix_hits = [item for item in tree if item.endswith("/" + path) or item == path]
                if len(suffix_hits) == 1 and suffix_hits[0] not in candidates:
                    candidates.append(suffix_hits[0])
        lowered = logs.lower()
        if any(marker in lowered for marker in DEPENDENCY_MARKERS):
            for manifest in ("package.json", "requirements.txt", "pyproject.toml"):
                if manifest in tree_set and manifest not in candidates:
                    candidates.append(manifest)
        return candidates[:MAX_CANDIDATE_FILES]

    def _apply_fix(
        self,
        workspace: Path,
        tree: list[str],
        fix: dict[str, Any],
        logs: str,
    ) -> dict[str, Any]:
        settings = get_settings()
        changes = fix.get("files") or []
        if len(changes) > settings.fixer_max_files:
            raise FixerError(
                f"Fix touches {len(changes)} files (limit {settings.fixer_max_files})"
            )
        dependency_incident = any(marker in logs.lower() for marker in DEPENDENCY_MARKERS)
        workspace_resolved = workspace.resolve()
        diffs: list[str] = []
        total_changed = 0
        primary_path = ""
        primary_content = ""

        for change in changes:
            rel = str(change["path"]).replace("\\", "/").lstrip("/")
            target = (workspace / rel).resolve()
            if not target.is_relative_to(workspace_resolved):
                raise FixerError(f"Fix path escapes the repository: {rel}")
            if rel.startswith(".git/") or rel == ".git":
                raise FixerError("Fix may not modify .git")
            name = PurePosixPath(rel).name
            if name in LOCKFILE_NAMES and not dependency_incident:
                raise FixerError(f"Fix may not modify lockfile {name} for this incident type")
            before = ""
            if target.exists():
                try:
                    before = target.read_text(encoding="utf-8")
                except UnicodeDecodeError as error:
                    raise FixerError(f"Fix targets a binary file: {rel}") from error
            elif change.get("action") != "create" and rel not in set(tree):
                raise FixerError(f"Fix updates a file that does not exist: {rel}")
            after = str(change["content"])
            diff_lines = list(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=f"a/{rel}",
                    tofile=f"b/{rel}",
                )
            )
            total_changed += sum(
                1 for line in diff_lines if line[:1] in "+-" and line[:3] not in ("+++", "---")
            )
            diffs.append("".join(diff_lines))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(after, encoding="utf-8", newline="\n")
            if not primary_path:
                primary_path = rel
                primary_content = after

        if total_changed == 0:
            raise FixerError("Generated fix does not change any lines")
        if total_changed > settings.fixer_max_changed_lines:
            raise FixerError(
                f"Fix changes {total_changed} lines (limit {settings.fixer_max_changed_lines})"
            )
        return {
            "diff": "\n".join(diffs)[:100_000],
            "primary_path": primary_path,
            "primary_content": primary_content[:100_000],
        }


fixer_service = CloneFixerService()
