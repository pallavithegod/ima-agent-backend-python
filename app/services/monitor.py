"""Commit monitor: polls GitHub for new commits on imported repositories and
triggers deployment inspection. Port of the Node backend's server.js loop."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from ..config import get_settings
from ..db import repo
from .crypto import decrypt_secret
from .remediation import remediation_service

logger = logging.getLogger("recallops.monitor")

TICK_SECONDS = 2


def _github_headers(token: str, etag: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "recallops-commit-monitor",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if etag:
        headers["If-None-Match"] = etag
    return headers


def _now_ms() -> float:
    return datetime.now(UTC).timestamp() * 1000


def _ms(value: str | None) -> float:
    if not value:
        return 0
    try:
        return datetime.fromisoformat(value).timestamp() * 1000
    except ValueError:
        return 0


def update_workflows_from_sync(user_id: int | str, payload: dict[str, Any]) -> None:
    for result in payload.get("results") or []:
        incident = result.get("incident") or {}
        if not incident.get("repository") or not incident.get("commit_sha"):
            continue
        if incident.get("pull_request_url"):
            status = "pull_request_created"
        elif incident.get("fixed_content"):
            status = "fix_generated"
        else:
            status = "failure_detected"
        repo.update_commit_workflow(
            user_id,
            incident["repository"],
            incident["commit_sha"],
            {
                "status": status,
                "deployment_id": result.get("deployment_id") or incident.get("deployment_id"),
                "incident_id": incident.get("id"),
                "pull_request_url": incident.get("pull_request_url"),
                "error": result.get("pull_request_error")
                or result.get("fix_error")
                or result.get("error"),
            },
        )


async def trigger_deployment_inspection(user_id: int | str) -> dict[str, Any]:
    providers = [
        provider
        for provider in ("vercel", "render")
        if repo.user_has_enabled_provider(user_id, provider)
    ]
    if not providers:
        return {"ok": False, "results": [], "providers": []}
    results: list[dict[str, Any]] = []
    provider_states = []
    ok = False
    for provider in providers:
        try:
            if provider == "vercel":
                payload = await remediation_service.sync_failed_deployments(str(user_id))
            else:
                payload = await remediation_service.sync_failed_render_deployments(str(user_id))
            results.extend(payload.get("results") or [])
            provider_states.append({"provider": provider, "ok": True, "error": None})
            ok = True
        except Exception as error:
            provider_states.append({"provider": provider, "ok": False, "error": str(error)})
    payload = {"ok": ok, "results": results, "providers": provider_states}
    if ok:
        update_workflows_from_sync(user_id, payload)
    return payload


async def _watch_repository(repository: dict[str, Any], now_ms: float) -> None:
    last_checked = _ms(repository.get("last_commit_checked_at"))
    interval = int(repository.get("poll_interval_seconds") or 30) * 1000
    if now_ms - last_checked < interval:
        return
    token = decrypt_secret(repository["github_access_token"])
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"https://api.github.com/repos/{repository['github_repository']}/commits?per_page=1",
            headers=_github_headers(token, repository.get("github_etag")),
        )
    checked_at = datetime.now(UTC).isoformat()
    if response.status_code == 304:
        repo.update_monitored_repository(
            repository["id"], {"last_commit_checked_at": checked_at}
        )
        return
    try:
        payload = response.json()
    except Exception:
        payload = []
    if response.status_code >= 400:
        message = payload.get("message") if isinstance(payload, dict) else None
        repo.update_monitored_repository(
            repository["id"],
            {
                "last_commit_checked_at": checked_at,
                "last_sync_error": message
                or f"GitHub commit check failed ({response.status_code})",
            },
        )
        return
    commit = payload[0] if isinstance(payload, list) and payload else None
    if not commit or not commit.get("sha"):
        return
    is_baseline = not repository.get("last_commit_sha")
    repo.update_monitored_repository(
        repository["id"],
        {
            "last_commit_sha": commit["sha"],
            "last_commit_at": ((commit.get("commit") or {}).get("author") or {}).get("date")
            or checked_at,
            "github_etag": response.headers.get("etag"),
            "last_commit_checked_at": checked_at,
            "last_sync_error": None,
        },
    )
    if is_baseline or commit["sha"] == repository.get("last_commit_sha"):
        return
    logger.info(
        "New commit detected", extra={"repository": repository["github_repository"], "sha": commit["sha"]}
    )
    repo.insert_commit_workflow(
        repository["user_id"],
        repository["github_repository"],
        commit["sha"],
        ((commit.get("commit") or {}).get("message") or "Commit detected").split("\n")[0],
        commit.get("html_url"),
    )
    tracked = repo.get_tracked_project_by_repo(
        repository["user_id"], repository["github_repository"]
    )
    if not tracked:
        repo.update_commit_workflow(
            repository["user_id"],
            repository["github_repository"],
            commit["sha"],
            {"status": "awaiting_deployment_access"},
        )
        return
    repo.update_commit_workflow(
        repository["user_id"],
        repository["github_repository"],
        commit["sha"],
        {"status": "deployment_check_started"},
    )
    sync = await trigger_deployment_inspection(repository["user_id"])
    if not sync["ok"]:
        errors = "; ".join(
            item["error"] for item in sync["providers"] if item.get("error")
        )
        repo.update_commit_workflow(
            repository["user_id"],
            repository["github_repository"],
            commit["sha"],
            {"status": "deployment_check_failed", "error": errors or "Deployment check failed"},
        )
        return
    matched = any(
        (result.get("incident") or {}).get("commit_sha") == commit["sha"]
        for result in sync["results"]
    )
    if not matched:
        repo.update_commit_workflow(
            repository["user_id"],
            repository["github_repository"],
            commit["sha"],
            {"status": "watching_deployment"},
        )


async def monitor_cycle() -> None:
    now_ms = _now_ms()
    repositories = await asyncio.to_thread(repo.repositories_with_github_connection)
    await asyncio.gather(
        *(_watch_repository(repository, now_ms) for repository in repositories),
        return_exceptions=True,
    )
    users = await asyncio.to_thread(repo.users_with_tracked_resources)
    due = [
        user
        for user in users
        if now_ms - _ms(user.get("last_synced_at")) >= int(user.get("poll_interval_seconds") or 30) * 1000
    ]

    async def sync_user(user: dict[str, Any]) -> None:
        result = await trigger_deployment_inspection(user["id"])
        await asyncio.to_thread(
            repo.update_repositories_synced,
            user["id"],
            None if result["ok"] else "Deployment sync failed",
        )

    await asyncio.gather(*(sync_user(user) for user in due), return_exceptions=True)


async def monitor_loop() -> None:
    settings = get_settings()
    if settings.monitor_interval_ms <= 0:
        logger.info("Commit monitor disabled (MONITOR_INTERVAL_MS=0)")
        return
    logger.info("Commit monitor started")
    running = False
    while True:
        await asyncio.sleep(TICK_SECONDS)
        if running:
            continue
        running = True
        try:
            await monitor_cycle()
        except Exception:
            logger.exception("Commit monitor cycle failed")
        finally:
            running = False
