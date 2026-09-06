from typing import Any

import httpx

from ..config import get_settings


class IntegrationConfigurationError(RuntimeError):
    pass


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



vercel_service = VercelService()
render_service = RenderService()
