import base64
import json
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ..config import get_settings


class IntegrationConfigurationError(RuntimeError):
    pass


class RuntimeConnectionService:
    async def get(self, authorization: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"{get_settings().node_backend_url.rstrip('/')}/api/integrations/runtime",
                headers={"Authorization": authorization},
            )
        if response.status_code == 409:
            raise IntegrationConfigurationError("Connect GitHub first")
        response.raise_for_status()
        payload = response.json()
        if not payload.get("githubToken"):
            raise IntegrationConfigurationError("Connect GitHub first")
        return payload


class VercelService:
    base_url = "https://api.vercel.com"

    def __init__(self) -> None:
        self.settings = get_settings()

    def _params(self, team_id: str | None, values: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(values or {})
        if team_id:
            params["teamId"] = team_id
        return params

    async def _get(self, token: str, team_id: str | None, path: str, params: dict[str, Any] | None = None) -> Any:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.base_url}{path}",
                params=self._params(team_id, params),
                headers={"Authorization": f"Bearer {token}"},
            )
        response.raise_for_status()
        return response.json()

    async def failed_deployments(self, token: str, project_id: str, team_id: str | None, limit: int = 20) -> list[dict[str, Any]]:
        payload = await self._get(
            token, team_id,
            "/v6/deployments",
            {
                "projectId": project_id,
                "target": self.settings.vercel_target,
                "state": "ERROR,CANCELED",
                "limit": limit,
            },
        )
        return payload.get("deployments", [])

    async def deployment(self, token: str, team_id: str | None, deployment_id: str) -> dict[str, Any]:
        return await self._get(
            token, team_id,
            f"/v13/deployments/{deployment_id}",
            {"withGitRepoInfo": "true"},
        )

    async def events(self, token: str, team_id: str | None, deployment_id: str, limit: int = 500) -> list[dict[str, Any]]:
        payload = await self._get(
            token, team_id,
            f"/v3/deployments/{deployment_id}/events",
            {"builds": 1, "direction": "backward", "limit": limit},
        )
        return payload if isinstance(payload, list) else payload.get("events", [])

    @staticmethod
    def git_metadata(deployment: dict[str, Any]) -> dict[str, str | None]:
        meta = deployment.get("meta") or {}
        git_source = deployment.get("gitSource") or {}
        owner = (
            meta.get("githubOrg")
            or meta.get("githubCommitOrg")
            or git_source.get("org")
            or git_source.get("owner")
        )
        repo = (
            meta.get("githubRepo")
            or meta.get("githubCommitRepo")
            or git_source.get("repo")
        )
        commit_sha = (
            meta.get("githubCommitSha")
            or meta.get("gitCommitSha")
            or git_source.get("sha")
        )
        git_ref = (
            meta.get("githubCommitRef")
            or meta.get("gitCommitRef")
            or git_source.get("ref")
        )
        return {
            "repository": f"{owner}/{repo}" if owner and repo else None,
            "commit_sha": commit_sha,
            "git_ref": git_ref,
            "commit_message": meta.get("githubCommitMessage")
            or meta.get("gitCommitMessage"),
        }

    @staticmethod
    def log_text(events: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for event in reversed(events):
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            text = (
                event.get("text")
                or event.get("message")
                or payload.get("text")
                or payload.get("message")
            )
            if text:
                lines.append(str(text))
        return "\n".join(lines)[-50_000:]


class RenderService:
    base_url = "https://api.render.com/v1"
    failed_statuses = {"build_failed", "update_failed", "canceled", "deactivated"}

    async def _get(
        self,
        token: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.base_url}{path}",
                params=params,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                },
            )
        response.raise_for_status()
        return response.json()

    async def failed_deployments(
        self,
        token: str,
        service_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        payload = await self._get(
            token,
            f"/services/{service_id}/deploys",
            {"limit": min(limit, 100)},
        )
        rows = payload if isinstance(payload, list) else payload.get("deploys", [])
        deploys = [row.get("deploy", row) for row in rows]
        return [
            deploy for deploy in deploys
            if str(deploy.get("status", "")).lower() in self.failed_statuses
        ]

    async def logs(
        self,
        token: str,
        owner_id: str,
        service_id: str,
        deploy: dict[str, Any],
    ) -> str:
        params: dict[str, Any] = {
            "ownerId": owner_id,
            "resource": service_id,
            "type": "build",
            "direction": "forward",
            "limit": 100,
        }
        if deploy.get("createdAt"):
            params["startTime"] = deploy["createdAt"]
        if deploy.get("finishedAt") or deploy.get("updatedAt"):
            params["endTime"] = deploy.get("finishedAt") or deploy.get("updatedAt")
        payload = await self._get(token, "/logs", params)
        rows = payload.get("logs", []) if isinstance(payload, dict) else payload
        return "\n".join(
            str(row.get("message") or row.get("text") or row.get("msg") or "")
            for row in rows
            if row.get("message") or row.get("text") or row.get("msg")
        )[-50_000:]

    @staticmethod
    def git_metadata(deploy: dict[str, Any]) -> dict[str, str | None]:
        commit = deploy.get("commit")
        if isinstance(commit, dict):
            commit_sha = commit.get("id") or commit.get("sha")
            commit_message = commit.get("message")
        else:
            commit_sha = commit or deploy.get("commitId")
            commit_message = None
        return {
            "commit_sha": str(commit_sha) if commit_sha else None,
            "commit_message": str(commit_message) if commit_message else None,
        }


class GitHubMCPService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _require_configuration(self, token: str) -> None:
        if not token or not self.settings.github_mcp_url:
            raise IntegrationConfigurationError(
                "A per-user GitHub token and GITHUB_MCP_URL are required"
            )

    async def call_tool(self, token: str, name: str, arguments: dict[str, Any]) -> Any:
        self._require_configuration(token)
        headers = {
            "Authorization": f"Bearer {token}",
            "X-MCP-Tools": (
                "get_commit,get_file_contents,create_branch,"
                "create_or_update_file,create_pull_request"
            ),
        }
        async with httpx.AsyncClient(
            headers=headers,
            timeout=30,
            follow_redirects=True,
        ) as http_client:
            async with streamable_http_client(
                self.settings.github_mcp_url,
                http_client=http_client,
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    if name not in {tool.name for tool in tools.tools}:
                        raise RuntimeError(f"GitHub MCP tool is unavailable: {name}")
                    result = await session.call_tool(name, arguments)
        if result.isError:
            message = self._text_content(result.content)
            raise RuntimeError(message or f"GitHub MCP tool failed: {name}")
        if result.structuredContent is not None:
            return result.structuredContent
        text = self._text_content(result.content)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    async def commit(self, token: str, repository: str, commit_sha: str) -> Any:
        owner, repo = self._repository_parts(repository)
        return await self.call_tool(
            token,
            "get_commit",
            {"owner": owner, "repo": repo, "sha": commit_sha, "page": 1, "perPage": 100},
        )

    async def file(self, token: str, repository: str, path: str, ref: str) -> dict[str, Any]:
        owner, repo = self._repository_parts(repository)
        payload = await self.call_tool(
            token,
            "get_file_contents",
            {"owner": owner, "repo": repo, "path": path, "ref": ref},
        )
        item = self._find_file_payload(payload)
        content = item.get("content", "")
        if item.get("encoding") == "base64":
            content = base64.b64decode(content).decode("utf-8")
        return {"content": content, "sha": item.get("sha")}

    async def create_draft_pull_request(
        self,
        token: str,
        repository: str,
        base_branch: str,
        file_path: str,
        current_file_sha: str,
        fixed_content: str,
        title: str,
        body: str,
        branch_name: str,
    ) -> str:
        owner, repo = self._repository_parts(repository)
        await self.call_tool(
            token,
            "create_branch",
            {
                "owner": owner,
                "repo": repo,
                "branch": branch_name,
                "from_branch": base_branch,
            },
        )
        await self.call_tool(
            token,
            "create_or_update_file",
            {
                "owner": owner,
                "repo": repo,
                "path": file_path,
                "message": title,
                "content": fixed_content,
                "branch": branch_name,
                "sha": current_file_sha,
            },
        )
        pull_request = await self.call_tool(
            token,
            "create_pull_request",
            {
                "owner": owner,
                "repo": repo,
                "title": title,
                "body": body,
                "head": branch_name,
                "base": base_branch,
                "draft": True,
            },
        )
        url = self._find_value(pull_request, {"html_url", "url"})
        if not url:
            raise RuntimeError("GitHub MCP created the pull request but returned no URL")
        return str(url)

    @staticmethod
    def _repository_parts(repository: str) -> tuple[str, str]:
        parts = repository.split("/", 1)
        if len(parts) != 2 or not all(parts):
            raise ValueError("GitHub repository must use owner/name format")
        return parts[0], parts[1]

    @staticmethod
    def _text_content(content: list[Any]) -> str:
        return "\n".join(
            item.text for item in content if getattr(item, "type", None) == "text"
        )

    @classmethod
    def _find_file_payload(cls, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            if "content" in value:
                return value
            for child in value.values():
                found = cls._find_file_payload(child)
                if found:
                    return found
        if isinstance(value, list):
            for child in value:
                found = cls._find_file_payload(child)
                if found:
                    return found
        return {}

    @classmethod
    def _find_value(cls, value: Any, keys: set[str]) -> Any:
        if isinstance(value, dict):
            for key in keys:
                if value.get(key):
                    return value[key]
            for child in value.values():
                found = cls._find_value(child, keys)
                if found:
                    return found
        if isinstance(value, list):
            for child in value:
                found = cls._find_value(child, keys)
                if found:
                    return found
        return None


vercel_service = VercelService()
render_service = RenderService()
github_mcp_service = GitHubMCPService()
runtime_connection_service = RuntimeConnectionService()
