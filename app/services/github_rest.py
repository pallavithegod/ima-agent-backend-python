"""GitHub REST API client. Replaces the remote GitHub MCP service: plain REST
covers everything the pipeline needs and works with any repo-scoped token."""

import base64
from typing import Any

import httpx

API_BASE = "https://api.github.com"


class GitHubRestService:
    @staticmethod
    def _headers(token: str, etag: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "incident-memory-agent",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if etag:
            headers["If-None-Match"] = etag
        return headers

    @staticmethod
    def _repository_parts(repository: str) -> tuple[str, str]:
        parts = repository.split("/", 1)
        if len(parts) != 2 or not all(parts):
            raise ValueError("GitHub repository must use owner/name format")
        return parts[0], parts[1]

    async def _request(
        self,
        method: str,
        token: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(
                method,
                f"{API_BASE}{path}",
                params=params,
                json=json_body,
                headers=self._headers(token),
            )
        if response.status_code >= 400:
            try:
                message = response.json().get("message")
            except Exception:
                message = None
            raise RuntimeError(message or f"GitHub request failed ({response.status_code})")
        return response.json()

    async def commit(self, token: str, repository: str, commit_sha: str) -> Any:
        owner, repo = self._repository_parts(repository)
        return await self._request(
            "GET", token, f"/repos/{owner}/{repo}/commits/{commit_sha}"
        )

    async def file(self, token: str, repository: str, path: str, ref: str) -> dict[str, Any]:
        owner, repo = self._repository_parts(repository)
        payload = await self._request(
            "GET", token, f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref}
        )
        if isinstance(payload, list):
            raise RuntimeError(f"Path is a directory, not a file: {path}")
        content = payload.get("content", "")
        if payload.get("encoding") == "base64":
            content = base64.b64decode(content).decode("utf-8")
        return {"content": content, "sha": payload.get("sha")}

    async def repository(self, token: str, repository: str) -> dict[str, Any]:
        owner, repo = self._repository_parts(repository)
        return await self._request("GET", token, f"/repos/{owner}/{repo}")

    async def user_repositories(self, token: str) -> list[dict[str, Any]]:
        return await self._request(
            "GET",
            token,
            "/user/repos",
            params={
                "per_page": 100,
                "sort": "updated",
                "affiliation": "owner,collaborator,organization_member",
            },
        )

    async def commits(self, token: str, repository: str, per_page: int = 12) -> list[dict[str, Any]]:
        owner, repo = self._repository_parts(repository)
        return await self._request(
            "GET", token, f"/repos/{owner}/{repo}/commits", params={"per_page": per_page}
        )

    async def create_pull_request(
        self,
        token: str,
        repository: str,
        head: str,
        base: str,
        title: str,
        body: str,
        draft: bool = True,
    ) -> str:
        owner, repo = self._repository_parts(repository)
        payload = await self._request(
            "POST",
            token,
            f"/repos/{owner}/{repo}/pulls",
            json_body={"title": title, "body": body, "head": head, "base": base, "draft": draft},
        )
        url = payload.get("html_url")
        if not url:
            raise RuntimeError("GitHub created the pull request but returned no URL")
        return str(url)

    async def single_file_pr(
        self,
        token: str,
        repository: str,
        base_branch: str,
        branch_name: str,
        file_path: str,
        current_file_sha: str,
        fixed_content: str,
        title: str,
        body: str,
    ) -> str:
        """Fallback PR flow that edits one file through the contents API."""
        owner, repo = self._repository_parts(repository)
        ref = await self._request(
            "GET", token, f"/repos/{owner}/{repo}/git/ref/heads/{base_branch}"
        )
        base_sha = (ref.get("object") or {}).get("sha")
        if not base_sha:
            raise RuntimeError(f"Could not resolve base branch: {base_branch}")
        await self._request(
            "POST",
            token,
            f"/repos/{owner}/{repo}/git/refs",
            json_body={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
        )
        body_payload: dict[str, Any] = {
            "message": title,
            "content": base64.b64encode(fixed_content.encode()).decode(),
            "branch": branch_name,
        }
        if current_file_sha:
            body_payload["sha"] = current_file_sha
        await self._request(
            "PUT", token, f"/repos/{owner}/{repo}/contents/{file_path}", json_body=body_payload
        )
        return await self.create_pull_request(
            token, repository, branch_name, base_branch, title, body, draft=True
        )


github_rest_service = GitHubRestService()
