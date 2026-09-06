"""In-process replacement for the Node backend's /api/integrations/runtime
endpoint: resolves a user's decrypted provider tokens and mappings."""

import json
from typing import Any

import httpx

from ..config import get_settings
from ..db import repo
from .credentials import connection
from .crypto import decrypt_secret, encrypt_secret
from .integrations import IntegrationConfigurationError


def _token_expiry(expires_in: Any) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) + timedelta(seconds=int(expires_in or 3600))).isoformat()


async def exchange_vercel_token(body: dict[str, str]) -> dict[str, Any]:
    import base64

    settings = get_settings()
    credentials_header = base64.b64encode(
        f"{settings.vercel_client_id}:{settings.vercel_client_secret}".encode()
    ).decode()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.vercel.com/login/oauth/token",
            headers={
                "Authorization": f"Basic {credentials_header}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=body,
        )
    payload = response.json() if response.content else {}
    if response.status_code >= 400:
        message = payload.get("error_description") or payload.get("error") or "token exchange failed"
        raise RuntimeError(f"Vercel OAuth error: {message}")
    return payload


async def vercel_access_token(user_id: int | str) -> str:
    """Decrypt the stored Vercel token, refreshing it first when close to expiry."""
    from datetime import UTC, datetime

    item = connection(user_id, "vercel")
    if not item:
        raise IntegrationConfigurationError("Connect Vercel first")
    expires_at = 0.0
    if item.get("token_expires_at"):
        expires_at = datetime.fromisoformat(item["token_expires_at"]).timestamp()
    if not item.get("refresh_token") or expires_at > datetime.now(UTC).timestamp() + 60:
        return decrypt_secret(item["access_token"])
    tokens = await exchange_vercel_token(
        {
            "grant_type": "refresh_token",
            "refresh_token": decrypt_secret(item["refresh_token"]),
        }
    )
    metadata = json.loads(item.get("metadata") or "{}")
    repo.upsert_connection_row(
        user_id,
        "vercel",
        encrypt_secret(tokens["access_token"]),
        item.get("account_id"),
        item.get("account_name"),
        metadata,
        encrypt_secret(tokens["refresh_token"]) if tokens.get("refresh_token") else None,
        _token_expiry(tokens.get("expires_in")),
    )
    return tokens["access_token"]


async def get_runtime(user_id: int | str) -> dict[str, Any]:
    """Same shape the remediation pipeline consumed from the Node backend."""
    github = connection(user_id, "github")
    if not github:
        raise IntegrationConfigurationError("Connect GitHub first")
    vercel = connection(user_id, "vercel")
    render = connection(user_id, "render")
    return {
        "githubToken": decrypt_secret(github["access_token"]),
        "vercelToken": await vercel_access_token(user_id) if vercel else None,
        "renderToken": decrypt_secret(render["access_token"]) if render else None,
        "projects": repo.list_tracked_projects(user_id, enabled_only=True),
        "renderServices": repo.list_render_service_rows(user_id, enabled_only=True),
    }
