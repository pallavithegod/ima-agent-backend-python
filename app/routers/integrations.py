"""Port of the Node backend's /api/integrations router: provider connections,
Vercel OAuth/PAT, Render, GitHub repository import/activity, provider health."""

import asyncio
import hashlib
import re
import secrets
from base64 import urlsafe_b64encode
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ..config import get_settings
from ..db import repo
from ..security import current_user
from ..services import credentials as creds
from ..services.crypto import decrypt_secret
from ..services.github_rest import github_rest_service
from ..services.integrations import IntegrationConfigurationError
from ..services.remediation import remediation_service
from ..services.runtime import exchange_vercel_token, vercel_access_token

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

REPO_NAME_RE = re.compile(r"^[^/]+/[^/]+$")
GITHUB_URL_RE = re.compile(r"github\.com[/:]([^/]+)/([^/#]+?)(?:\.git)?$", re.IGNORECASE)


def _render_headers(token: str) -> dict[str, str]:
    return {"Accept": "application/json", "Authorization": f"Bearer {token}"}


def _vercel_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _checked_fetch(url: str, headers: dict[str, str]) -> Any:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, headers=headers)
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if response.status_code >= 400:
        message = payload.get("message") if isinstance(payload, dict) else None
        error_field = payload.get("error") if isinstance(payload, dict) else None
        if not message and isinstance(error_field, dict):
            message = error_field.get("message")
        raise RuntimeError(message or f"Provider request failed ({response.status_code})")
    return payload


async def _optional_fetch(url: str, headers: dict[str, str]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, headers=headers)
    try:
        payload = response.json()
    except Exception:
        payload = {}
    return {"ok": response.status_code < 400, "status": response.status_code, "payload": payload}


async def inspect_provider(user_id: str, provider: str) -> dict[str, Any]:
    """In-process replacement for the Node → Python HTTP hop."""
    if provider == "vercel":
        return await remediation_service.sync_failed_deployments(user_id)
    if provider == "render":
        return await remediation_service.sync_failed_render_deployments(user_id)
    raise RuntimeError("Unsupported deployment provider")


def repository_shape(
    repo_payload: dict[str, Any],
    imported: bool = False,
    vercel_project: dict[str, Any] | None = None,
) -> dict[str, Any]:
    owner = repo_payload.get("owner") or {}
    return {
        "id": str(repo_payload.get("id")),
        "fullName": repo_payload.get("full_name"),
        "name": repo_payload.get("name"),
        "owner": owner.get("login") or str(repo_payload.get("full_name", "")).split("/")[0],
        "ownerAvatar": owner.get("avatar_url"),
        "private": bool(repo_payload.get("private")),
        "url": repo_payload.get("html_url"),
        "description": repo_payload.get("description"),
        "language": repo_payload.get("language"),
        "defaultBranch": repo_payload.get("default_branch"),
        "updatedAt": repo_payload.get("pushed_at") or repo_payload.get("updated_at"),
        "imported": imported,
        "vercelProject": vercel_project,
    }


def _iso_from_ms(value: Any) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, UTC).isoformat()
    except (ValueError, TypeError):
        return str(value)


def vercel_project_shape(project: dict[str, Any], team_id: str | None, scope: str | None) -> dict[str, Any]:
    link = project.get("link") or {}
    owner = link.get("org") or link.get("orgId") or link.get("owner")
    repo_name = link.get("repo") or link.get("repoName")
    return {
        "id": project.get("id"),
        "name": project.get("name"),
        "teamId": team_id,
        "scope": scope,
        "framework": project.get("framework"),
        "updatedAt": _iso_from_ms(project.get("updatedAt")),
        "githubRepository": f"{owner}/{repo_name}" if owner and repo_name else repo_name,
    }


def project_from_deployment(deployment: dict[str, Any]) -> dict[str, Any] | None:
    meta = deployment.get("meta") or {}
    owner = meta.get("githubOrg") or meta.get("githubCommitOrg")
    repo_name = meta.get("githubRepo") or meta.get("githubCommitRepo")
    project = deployment.get("project") or {}
    team = deployment.get("team") or {}
    project_id = deployment.get("projectId") or project.get("id") or deployment.get("name")
    if not project_id:
        return None
    return {
        "id": project_id,
        "name": deployment.get("name") or project.get("name") or project_id,
        "teamId": deployment.get("teamId") or team.get("id"),
        "scope": team.get("name") or "Deployment access",
        "framework": None,
        "updatedAt": _iso_from_ms(deployment.get("createdAt")),
        "githubRepository": f"{owner}/{repo_name}" if owner and repo_name else repo_name,
    }


def github_repository_from_url(value: Any) -> str | None:
    if not value:
        return None
    match = GITHUB_URL_RE.search(str(value))
    return f"{match.group(1)}/{match.group(2)}" if match else None


def render_service_shape(item: dict[str, Any]) -> dict[str, Any]:
    service = item.get("service") or item
    details = service.get("serviceDetails") or {}
    owner = service.get("owner") or {}
    return {
        "id": service.get("id"),
        "name": service.get("name"),
        "ownerId": service.get("ownerId") or owner.get("id"),
        "githubRepository": github_repository_from_url(service.get("repo"))
        or github_repository_from_url(service.get("repository"))
        or github_repository_from_url(details.get("repo")),
        "type": service.get("type") or details.get("runtime") or "service",
        "url": details.get("url") or service.get("url"),
    }


async def render_services_for_user(user_id: str) -> list[dict[str, Any]]:
    item = creds.connection(user_id, "render")
    if not item:
        raise RuntimeError("Connect Render first")
    payload = await _checked_fetch(
        "https://api.render.com/v1/services?limit=100",
        _render_headers(decrypt_secret(item["access_token"])),
    )
    rows = payload if isinstance(payload, list) else payload.get("services", [])
    return [shape for shape in map(render_service_shape, rows) if shape["id"]]


def match_render_repositories(user_id: str, services: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches = []
    for repository in repo.list_monitored_repositories(user_id):
        full_name = repository["github_repository"].lower()
        repo_name = full_name.split("/")[-1]
        service = next(
            (item for item in services if (item.get("githubRepository") or "").lower() == full_name),
            None,
        )
        if not service:
            same_name = [
                item
                for item in services
                if (item.get("githubRepository") or "").split("/")[-1].lower() == repo_name
            ]
            service = same_name[0] if len(same_name) == 1 else None
        if not service:
            continue
        repo.upsert_render_service(
            user_id, service["id"], service["name"], service.get("ownerId"),
            repository["github_repository"],
        )
        matches.append({"repository": repository["github_repository"], "service": service})
    return matches


async def vercel_projects_for_user(user_id: str) -> dict[str, Any]:
    token = await vercel_access_token(user_id)
    personal = await _optional_fetch(
        "https://api.vercel.com/v9/projects?limit=100", _vercel_headers(token)
    )
    projects = (
        [vercel_project_shape(p, None, "Personal") for p in personal["payload"].get("projects", [])]
        if personal["ok"]
        else []
    )
    teams_result = await _optional_fetch(
        "https://api.vercel.com/v2/teams?limit=100", _vercel_headers(token)
    )
    teams = teams_result["payload"].get("teams", []) if teams_result["ok"] else []

    async def team_projects(team: dict[str, Any]) -> list[dict[str, Any]]:
        team_id = team.get("id")
        query = f"?limit=100&teamId={team_id}" if team_id else "?limit=100"
        result = await _optional_fetch(
            f"https://api.vercel.com/v9/projects{query}", _vercel_headers(token)
        )
        if not result["ok"]:
            return []
        return [
            vercel_project_shape(p, team_id, team.get("name"))
            for p in result["payload"].get("projects", [])
        ]

    groups = await asyncio.gather(*(team_projects(team) for team in teams))
    deployment_result = await _optional_fetch(
        "https://api.vercel.com/v6/deployments?limit=100", _vercel_headers(token)
    )
    deployment_projects = (
        [
            project
            for project in map(project_from_deployment, deployment_result["payload"].get("deployments", []))
            if project
        ]
        if deployment_result["ok"]
        else []
    )
    unique: dict[str, dict[str, Any]] = {}
    for project in [*projects, *(p for group in groups for p in group), *deployment_projects]:
        key = f"{project.get('teamId') or 'personal'}:{project['id']}"
        current = unique.get(key) or {}
        unique[key] = {
            **current,
            **project,
            "githubRepository": project.get("githubRepository") or current.get("githubRepository"),
        }
    return {
        "projects": list(unique.values()),
        "teamAccessLimited": not teams_result["ok"],
        "personalAccessLimited": not personal["ok"],
        "deploymentAccessLimited": not deployment_result["ok"],
    }


def match_imported_repositories(user_id: str, projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches = []
    for repository in repo.list_monitored_repositories(user_id):
        full_name = repository["github_repository"].lower()
        name = full_name.split("/")[-1]
        project = next(
            (item for item in projects if (item.get("githubRepository") or "").lower() == full_name),
            None,
        )
        if not project:
            same_name = [
                item
                for item in projects
                if (item.get("githubRepository") or "").split("/")[-1].lower() == name
            ]
            project = same_name[0] if len(same_name) == 1 else None
        if not project:
            continue
        repo.upsert_tracked_project(
            user_id, project["id"], project["name"], project.get("teamId"),
            repository["github_repository"],
        )
        matches.append({"repository": repository["github_repository"], "project": project})
    return matches


async def vercel_health_for_user(user_id: str) -> list[dict[str, Any]]:
    if not creds.connection(user_id, "vercel"):
        return []
    token = await vercel_access_token(user_id)
    discovery = await vercel_projects_for_user(user_id)
    tracked = repo.list_tracked_projects(user_id, enabled_only=True)
    resources: dict[str, dict[str, Any]] = {}
    for project in discovery["projects"]:
        key = f"{project.get('githubRepository') or 'unlinked'}:{project.get('name')}".lower()
        current = resources.get(key) or {}
        resources[key] = {
            **current,
            **project,
            "teamId": project.get("teamId") or current.get("teamId"),
            "githubRepository": project.get("githubRepository") or current.get("githubRepository"),
        }
    for project in tracked:
        key = f"{project.get('github_repository') or 'unlinked'}:{project['vercel_project_name']}".lower()
        if key not in resources:
            resources[key] = {
                "id": project["vercel_project_id"],
                "name": project["vercel_project_name"],
                "teamId": project["vercel_team_id"],
                "githubRepository": project["github_repository"],
            }

    async def health(project: dict[str, Any]) -> dict[str, Any]:
        params = f"projectId={project['id']}&limit=1"
        if project.get("teamId"):
            params += f"&teamId={project['teamId']}"
        result = await _optional_fetch(
            f"https://api.vercel.com/v6/deployments?{params}", _vercel_headers(token)
        )
        deployment = (result["payload"].get("deployments") or [None])[0] if result["ok"] else None
        mapping = next(
            (item for item in tracked if item["vercel_project_id"] == project["id"]), None
        )
        repository = (
            project.get("githubRepository")
            or (mapping or {}).get("github_repository")
        )
        meta = (deployment or {}).get("meta") or {}
        return {
            "provider": "vercel",
            "resourceId": project["id"],
            "resourceName": project.get("name"),
            "ownerId": project.get("teamId"),
            "repository": repository,
            "repositoryImported": bool(
                repository and repo.get_monitored_repository(user_id, repository)
            ),
            "tracked": bool(mapping),
            "accessible": result["ok"],
            "latestDeployment": {
                "id": deployment.get("uid") or deployment.get("id"),
                "status": deployment.get("readyState")
                or deployment.get("state")
                or deployment.get("status")
                or "UNKNOWN",
                "commitSha": meta.get("githubCommitSha") or meta.get("gitCommitSha"),
                "message": meta.get("githubCommitMessage") or meta.get("gitCommitMessage"),
                "createdAt": _iso_from_ms(deployment.get("createdAt")),
                "url": f"https://{deployment['url']}" if deployment.get("url") else None,
            }
            if deployment
            else None,
        }

    return list(await asyncio.gather(*(health(project) for project in resources.values())))


async def render_health_for_user(user_id: str) -> list[dict[str, Any]]:
    item = creds.connection(user_id, "render")
    if not item:
        return []
    token = decrypt_secret(item["access_token"])
    services = await render_services_for_user(user_id)
    tracked = repo.list_render_service_rows(user_id, enabled_only=True)
    resources: dict[str, dict[str, Any]] = {service["id"]: service for service in services}
    for service in tracked:
        if service["render_service_id"] not in resources:
            resources[service["render_service_id"]] = {
                "id": service["render_service_id"],
                "name": service["render_service_name"],
                "ownerId": service["render_owner_id"],
                "githubRepository": service["github_repository"],
                "url": None,
            }

    async def health(service: dict[str, Any]) -> dict[str, Any]:
        result = await _optional_fetch(
            f"https://api.render.com/v1/services/{service['id']}/deploys?limit=1",
            _render_headers(token),
        )
        row = None
        if result["ok"]:
            payload = result["payload"]
            row = payload[0] if isinstance(payload, list) and payload else (
                (payload.get("deploys") or [None])[0] if isinstance(payload, dict) else None
            )
        deployment = (row or {}).get("deploy") if isinstance(row, dict) and "deploy" in (row or {}) else row
        mapping = next(
            (m for m in tracked if m["render_service_id"] == service["id"]), None
        )
        repository = service.get("githubRepository") or (mapping or {}).get("github_repository")
        commit = (deployment or {}).get("commit")
        commit_sha = (
            (commit.get("id") or commit.get("sha")) if isinstance(commit, dict)
            else commit or (deployment or {}).get("commitId")
        )
        return {
            "provider": "render",
            "resourceId": service["id"],
            "resourceName": service.get("name"),
            "ownerId": service.get("ownerId"),
            "repository": repository,
            "repositoryImported": bool(
                repository and repo.get_monitored_repository(user_id, repository)
            ),
            "tracked": bool(mapping),
            "accessible": result["ok"],
            "latestDeployment": {
                "id": deployment.get("id"),
                "status": deployment.get("status") or "UNKNOWN",
                "commitSha": commit_sha,
                "message": commit.get("message") if isinstance(commit, dict) else None,
                "createdAt": deployment.get("createdAt"),
                "url": service.get("url"),
            }
            if deployment
            else None,
        }

    return list(await asyncio.gather(*(health(service) for service in resources.values())))


# ---------------------------------------------------------------------------
# Routes


@router.get("/status")
def status(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    settings = get_settings()
    connections = repo.list_connections(user["sub"])
    projects = repo.list_tracked_projects(user["sub"])
    render_services = repo.list_render_service_rows(user["sub"])
    repositories = repo.list_monitored_repositories(user["sub"])
    vercel_connection = next((c for c in connections if c["provider"] == "vercel"), None)
    vercel_metadata = creds.connection_metadata(vercel_connection)
    return {
        "connections": [
            {
                "provider": item["provider"],
                "account_id": item["account_id"],
                "account_name": item["account_name"],
                "metadata": creds.connection_metadata(item),
                "updated_at": item["updated_at"],
            }
            for item in connections
        ],
        "projects": projects,
        "renderServices": render_services,
        "repositories": repositories,
        "monitorActive": any(p["enabled"] == 1 for p in projects)
        or any(s["enabled"] == 1 for s in render_services),
        "vercelProjectAccessEnabled": vercel_metadata.get("auth_mode") == "access_token"
        or len(projects) > 0,
        "vercelAuthorizationConfigured": settings.vercel_oauth_configured,
    }


@router.get("/github/repositories")
async def github_repositories(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    item = creds.connection(user["sub"], "github")
    if not item:
        raise HTTPException(status_code=409, detail="Connect GitHub first")
    try:
        repos = await github_rest_service.user_repositories(decrypt_secret(item["access_token"]))
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    imported = {
        row["github_repository"].lower() for row in repo.list_monitored_repositories(user["sub"])
    }
    tracked = repo.list_tracked_projects(user["sub"], enabled_only=True)
    projects = {
        project["github_repository"].lower(): {
            "id": project["vercel_project_id"],
            "name": project["vercel_project_name"],
            "teamId": project["vercel_team_id"],
        }
        for project in tracked
    }
    return {
        "repositories": [
            repository_shape(
                item,
                item["full_name"].lower() in imported,
                projects.get(item["full_name"].lower()),
            )
            for item in repos
        ]
    }


@router.get("/vercel/start")
def vercel_start(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    settings = get_settings()
    if not settings.vercel_oauth_configured:
        raise HTTPException(status_code=503, detail="Vercel authorization is not configured")
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    nonce = secrets.token_urlsafe(24)
    repo.create_oauth_state(state, user["sub"], "vercel", verifier, nonce)
    params = httpx.QueryParams(
        {
            "client_id": settings.vercel_client_id,
            "redirect_uri": settings.vercel_callback_url,
            "response_type": "code",
            "scope": settings.vercel_scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return {"url": f"https://vercel.com/oauth/authorize?{params}"}


@router.get("/vercel/callback")
async def vercel_callback(request: Request) -> RedirectResponse:
    settings = get_settings()
    state = repo.consume_oauth_state(str(request.query_params.get("state") or ""), "vercel")
    if not state:
        raise HTTPException(status_code=400, detail="Invalid or expired Vercel authorization state")
    if request.query_params.get("error"):
        params = httpx.QueryParams(
            {
                "integration": "vercel",
                "status": "error",
                "error": str(
                    request.query_params.get("error_description")
                    or request.query_params.get("error")
                ),
            }
        )
        return RedirectResponse(f"{settings.frontend_url}/?{params}")
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Vercel did not return an authorization code")
    tokens = await exchange_vercel_token(
        {
            "grant_type": "authorization_code",
            "code": str(code),
            "redirect_uri": settings.vercel_callback_url,
            "code_verifier": state["code_verifier"] or "",
        }
    )
    profile = await _checked_fetch(
        "https://api.vercel.com/login/oauth/userinfo", _vercel_headers(tokens["access_token"])
    )
    from datetime import timedelta

    creds.upsert_connection(
        state["user_id"],
        "vercel",
        tokens["access_token"],
        profile.get("sub"),
        profile.get("name") or profile.get("preferred_username") or profile.get("email"),
        {"email": profile.get("email"), "scope": tokens.get("scope")},
        tokens.get("refresh_token"),
        (datetime.now(UTC) + timedelta(seconds=int(tokens.get("expires_in") or 3600))).isoformat(),
    )
    try:
        discovery = await vercel_projects_for_user(str(state["user_id"]))
        match_imported_repositories(str(state["user_id"]), discovery["projects"])
    except Exception:
        pass
    return RedirectResponse(f"{settings.frontend_url}/")


class VercelTokenPayload(BaseModel):
    accessToken: str = Field(min_length=20)


@router.post("/vercel/token", status_code=201)
async def vercel_token(
    payload: VercelTokenPayload, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    result = await _optional_fetch(
        "https://api.vercel.com/v2/user", _vercel_headers(payload.accessToken)
    )
    if not result["ok"]:
        raise HTTPException(status_code=401, detail="Vercel access token is invalid")
    vercel_user = result["payload"].get("user") or result["payload"]
    creds.upsert_connection(
        user["sub"],
        "vercel",
        payload.accessToken,
        vercel_user.get("id") or vercel_user.get("uid") or "vercel-user",
        vercel_user.get("name")
        or vercel_user.get("username")
        or vercel_user.get("email")
        or "Vercel account",
        {"auth_mode": "access_token", "email": vercel_user.get("email")},
    )
    repo.clear_connection_refresh(user["sub"], "vercel")
    discovery = await vercel_projects_for_user(user["sub"])
    matches = match_imported_repositories(user["sub"], discovery["projects"])
    return {
        "connected": True,
        "accountName": vercel_user.get("name") or vercel_user.get("username") or "Vercel account",
        "projects": len(discovery["projects"]),
        "matches": len(matches),
    }


@router.get("/vercel/projects")
async def vercel_projects(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if not creds.connection(user["sub"], "vercel"):
        raise HTTPException(status_code=409, detail="Connect Vercel first")
    return await vercel_projects_for_user(user["sub"])


@router.post("/repositories/rematch")
async def repositories_rematch(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if not creds.connection(user["sub"], "vercel"):
        raise HTTPException(status_code=409, detail="Connect Vercel first")
    discovery = await vercel_projects_for_user(user["sub"])
    matches = match_imported_repositories(user["sub"], discovery["projects"])
    return {**discovery, "matches": matches}


class ImportPayload(BaseModel):
    fullName: str = Field(pattern=r"^[^/]+/[^/]+$")
    id: str | None = None
    private: bool | None = None
    defaultBranch: str | None = None


@router.post("/repositories/import", status_code=201)
async def import_repository(
    payload: ImportPayload, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    github = creds.connection(user["sub"], "github")
    if not github:
        raise HTTPException(status_code=409, detail="Connect GitHub first")
    try:
        repo_payload = await github_rest_service.repository(
            decrypt_secret(github["access_token"]), payload.fullName
        )
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    repo.upsert_monitored_repository(
        user["sub"],
        repo_payload["full_name"],
        str(repo_payload["id"]),
        bool(repo_payload.get("private")),
        repo_payload.get("default_branch"),
    )

    matched_project = None
    if creds.connection(user["sub"], "vercel"):
        discovery = await vercel_projects_for_user(user["sub"])
        full_name = repo_payload["full_name"].lower()
        matched_project = next(
            (
                project
                for project in discovery["projects"]
                if (project.get("githubRepository") or "").lower() == full_name
            ),
            None,
        )
        if not matched_project:
            same_name = [
                project
                for project in discovery["projects"]
                if (project.get("githubRepository") or "").split("/")[-1].lower()
                == repo_payload["name"].lower()
            ]
            matched_project = same_name[0] if len(same_name) == 1 else None
        if matched_project:
            repo.upsert_tracked_project(
                user["sub"],
                matched_project["id"],
                matched_project["name"],
                matched_project.get("teamId"),
                repo_payload["full_name"],
            )
    matched_render = False
    if creds.connection(user["sub"], "render"):
        try:
            render_matches = match_render_repositories(
                user["sub"], await render_services_for_user(user["sub"])
            )
            matched_render = any(
                match["repository"].lower() == repo_payload["full_name"].lower()
                for match in render_matches
            )
        except Exception:
            pass
    providers = [*(["vercel"] if matched_project else []), *(["render"] if matched_render else [])]
    inspections = await asyncio.gather(
        *(inspect_provider(user["sub"], provider) for provider in providers),
        return_exceptions=True,
    )
    return {
        "repository": repository_shape(repo_payload, True, matched_project),
        "matchedProject": matched_project,
        "requiresProjectMapping": bool(
            creds.connection(user["sub"], "vercel") and not matched_project
        ),
        "inspections": [
            {
                "provider": providers[index],
                "started": not isinstance(result, BaseException),
                "result": None if isinstance(result, BaseException) else result,
                "error": str(result) if isinstance(result, BaseException) else None,
            }
            for index, result in enumerate(inspections)
        ],
    }


class RepositorySettingsPayload(BaseModel):
    pollIntervalSeconds: int = Field(ge=2, le=3600)


@router.patch("/repositories/{owner}/{repo_name}/settings")
def repository_settings(
    owner: str,
    repo_name: str,
    payload: RepositorySettingsPayload,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    if not repo.update_repository_settings(
        user["sub"], f"{owner}/{repo_name}", payload.pollIntervalSeconds
    ):
        raise HTTPException(status_code=404, detail="Imported repository not found")
    return {"updated": True, "pollIntervalSeconds": payload.pollIntervalSeconds}


class RenderConnectPayload(BaseModel):
    apiKey: str = Field(min_length=20)


@router.post("/render/connect", status_code=201)
async def render_connect(
    payload: RenderConnectPayload, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    result = await _optional_fetch(
        "https://api.render.com/v1/owners?limit=1", _render_headers(payload.apiKey)
    )
    if not result["ok"]:
        raise HTTPException(
            status_code=401, detail="Render API key is invalid or lacks account access"
        )
    payload_rows = result["payload"]
    owner = None
    if isinstance(payload_rows, list) and payload_rows:
        owner = payload_rows[0].get("owner") or payload_rows[0]
    creds.upsert_connection(
        user["sub"],
        "render",
        payload.apiKey,
        (owner or {}).get("id") or "render-account",
        (owner or {}).get("name") or (owner or {}).get("email") or "Render account",
        {},
    )
    services = await render_services_for_user(user["sub"])
    matches = match_render_repositories(user["sub"], services)
    return {
        "connected": True,
        "accountName": (owner or {}).get("name") or "Render account",
        "services": len(services),
        "matches": len(matches),
    }


@router.get("/render/services")
async def render_services(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    try:
        return {"services": await render_services_for_user(user["sub"])}
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/render/rematch")
async def render_rematch(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    try:
        services = await render_services_for_user(user["sub"])
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"services": services, "matches": match_render_repositories(user["sub"], services)}


@router.get("/provider-health")
async def provider_health(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    async def safe(coro, provider: str):
        try:
            return await coro
        except Exception as error:
            return [{"provider": provider, "accessible": False, "error": str(error)}]

    vercel, render = await asyncio.gather(
        safe(vercel_health_for_user(user["sub"]), "vercel"),
        safe(render_health_for_user(user["sub"]), "render"),
    )
    return {"resources": [*vercel, *render]}


class TrackPayload(BaseModel):
    provider: str = Field(pattern="^(vercel|render)$")
    resourceId: str = Field(min_length=1)
    resourceName: str = Field(min_length=1)
    ownerId: str | None = None
    repository: str = Field(pattern=r"^[^/]+/[^/]+$")


@router.post("/provider-health/track", status_code=201)
async def track_resource(
    payload: TrackPayload, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    if not repo.get_monitored_repository(user["sub"], payload.repository):
        raise HTTPException(
            status_code=409,
            detail="Import the linked GitHub repository before monitoring this deployment",
        )
    if payload.provider == "vercel":
        repo.upsert_tracked_project(
            user["sub"], payload.resourceId, payload.resourceName, payload.ownerId,
            payload.repository,
        )
    else:
        repo.upsert_render_service(
            user["sub"], payload.resourceId, payload.resourceName, payload.ownerId,
            payload.repository,
        )
    inspection = None
    inspection_error = None
    try:
        inspection = await inspect_provider(user["sub"], payload.provider)
    except Exception as error:
        inspection_error = str(error)
    return {"tracked": True, "inspection": inspection, "inspectionError": inspection_error}


@router.post("/provider-health/{provider}/inspect")
async def provider_inspect(
    provider: str, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    if provider not in ("vercel", "render"):
        raise HTTPException(status_code=400, detail="Unsupported deployment provider")
    try:
        return await inspect_provider(user["sub"], provider)
    except IntegrationConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.delete("/connections/{provider}", status_code=204)
def delete_provider_connection(
    provider: str, user: dict[str, Any] = Depends(current_user)
) -> None:
    if provider not in ("vercel", "render"):
        raise HTTPException(status_code=400, detail="This provider cannot be disconnected here")
    creds.delete_connection(user["sub"], provider)


@router.get("/repositories/{owner}/{repo_name}/activity")
async def repository_activity(
    owner: str, repo_name: str, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    full_name = f"{owner}/{repo_name}"
    github = creds.connection(user["sub"], "github")
    if not github:
        raise HTTPException(status_code=409, detail="Connect GitHub first")
    github_token = decrypt_secret(github["access_token"])
    try:
        repository, commits = await asyncio.gather(
            github_rest_service.repository(github_token, full_name),
            github_rest_service.commits(github_token, full_name, per_page=12),
        )
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    tracked = repo.get_tracked_project_by_repo(user["sub"], full_name)
    workflows = repo.list_commit_workflows(user["sub"], full_name)
    deployments: list[dict[str, Any]] = []
    latest_logs: list[dict[str, Any]] = []
    if tracked and creds.connection(user["sub"], "vercel"):
        token = await vercel_access_token(user["sub"])
        params = f"projectId={tracked['vercel_project_id']}&limit=12"
        if tracked.get("vercel_team_id"):
            params += f"&teamId={tracked['vercel_team_id']}"
        payload = await _checked_fetch(
            f"https://api.vercel.com/v6/deployments?{params}", _vercel_headers(token)
        )
        deployments = [
            {
                "id": item.get("uid") or item.get("id"),
                "name": item.get("name"),
                "state": item.get("readyState") or item.get("state") or item.get("status"),
                "url": f"https://{item['url']}" if item.get("url") else None,
                "createdAt": _iso_from_ms(item.get("createdAt")),
                "commitSha": (item.get("meta") or {}).get("githubCommitSha")
                or (item.get("meta") or {}).get("gitCommitSha"),
                "commitMessage": (item.get("meta") or {}).get("githubCommitMessage")
                or (item.get("meta") or {}).get("gitCommitMessage"),
            }
            for item in payload.get("deployments", [])
        ]
        latest = deployments[0] if deployments else None
        if latest and latest.get("id"):
            event_params = "builds=1&direction=backward&limit=160"
            if tracked.get("vercel_team_id"):
                event_params += f"&teamId={tracked['vercel_team_id']}"
            events = await _checked_fetch(
                f"https://api.vercel.com/v3/deployments/{latest['id']}/events?{event_params}",
                _vercel_headers(token),
            )
            rows = events if isinstance(events, list) else events.get("events", [])
            shaped = [
                {
                    "id": str(event.get("id") or event.get("created") or secrets.token_hex(8)),
                    "createdAt": _iso_from_ms(event.get("created")),
                    "level": event.get("type") or (event.get("payload") or {}).get("level") or "info",
                    "text": event.get("text")
                    or event.get("message")
                    or (event.get("payload") or {}).get("text")
                    or (event.get("payload") or {}).get("message")
                    or "",
                }
                for event in rows
            ]
            latest_logs = [event for event in shaped if event["text"]][::-1][-120:]
    return {
        "repository": repository_shape(
            repository,
            True,
            {
                "id": tracked["vercel_project_id"],
                "name": tracked["vercel_project_name"],
                "teamId": tracked["vercel_team_id"],
            }
            if tracked
            else None,
        ),
        "commits": [
            {
                "sha": commit.get("sha"),
                "message": (
                    ((commit.get("commit") or {}).get("message") or "Commit").split("\n")[0]
                ),
                "author": (commit.get("author") or {}).get("login")
                or ((commit.get("commit") or {}).get("author") or {}).get("name")
                or "Unknown",
                "avatar": (commit.get("author") or {}).get("avatar_url"),
                "createdAt": ((commit.get("commit") or {}).get("author") or {}).get("date"),
                "url": commit.get("html_url"),
            }
            for commit in commits
        ],
        "deployments": deployments,
        "latestLogs": latest_logs,
        "workflows": [
            {
                "commit_sha": item["commit_sha"],
                "commit_message": item["commit_message"],
                "commit_url": item["commit_url"],
                "status": item["status"],
                "deployment_id": item["deployment_id"],
                "incident_id": item["incident_id"],
                "pull_request_url": item["pull_request_url"],
                "error": item["error"],
                "created_at": item["created_at"],
                "updated_at": item["updated_at"],
            }
            for item in workflows
        ],
    }


class TrackedProjectPayload(BaseModel):
    vercelProjectId: str = Field(min_length=1)
    vercelProjectName: str = Field(min_length=1)
    vercelTeamId: str | None = None
    githubRepository: str = Field(pattern=r"^[^/]+/[^/]+$")


@router.post("/tracked-projects", status_code=201)
async def create_tracked_project(
    payload: TrackedProjectPayload, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    repo.upsert_tracked_project(
        user["sub"],
        payload.vercelProjectId,
        payload.vercelProjectName,
        payload.vercelTeamId,
        payload.githubRepository,
    )
    inspection = None
    inspection_error = None
    try:
        inspection = await inspect_provider(user["sub"], "vercel")
    except Exception as error:
        inspection_error = str(error)
    return {"tracked": True, "inspection": inspection, "inspectionError": inspection_error}


@router.delete("/tracked-projects/{project_id}", status_code=204)
def remove_tracked_project(
    project_id: str, user: dict[str, Any] = Depends(current_user)
) -> None:
    repo.delete_tracked_project(user["sub"], project_id)


# Deployment sync endpoints (previously in main.py; the Node monitor called
# these over HTTP — the frontend still uses them for manual syncs).


@router.post("/vercel/sync")
async def sync_vercel(
    limit: int = 20, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    try:
        return await remediation_service.sync_failed_deployments(
            str(user["sub"]), limit=max(1, min(limit, 100))
        )
    except IntegrationConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post("/render/sync")
async def sync_render(
    limit: int = 20, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    try:
        return await remediation_service.sync_failed_render_deployments(
            str(user["sub"]), limit=max(1, min(limit, 100))
        )
    except IntegrationConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
