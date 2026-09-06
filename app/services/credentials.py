"""Provider connection storage (encrypted at rest). Port of the Node backend's
providerCredentials.js, minus the Firestore mirror."""

import json
from typing import Any

from ..db import repo
from .crypto import decrypt_secret, encrypt_secret


def connection(user_id: int | str, provider: str) -> dict[str, Any] | None:
    return repo.get_connection(user_id, provider)


def access_token(user_id: int | str, provider: str) -> str | None:
    item = repo.get_connection(user_id, provider)
    return decrypt_secret(item["access_token"]) if item else None


def upsert_connection(
    user_id: int | str,
    provider: str,
    token: str,
    account_id: str | None,
    account_name: str | None,
    metadata: dict[str, Any] | None = None,
    refresh_token: str | None = None,
    token_expires_at: str | None = None,
) -> None:
    repo.upsert_connection_row(
        user_id,
        provider,
        encrypt_secret(token),
        account_id,
        account_name,
        metadata or {},
        encrypt_secret(refresh_token) if refresh_token else None,
        token_expires_at,
    )


def delete_connection(user_id: int | str, provider: str) -> None:
    repo.delete_connection(user_id, provider)


def connection_metadata(item: dict[str, Any] | None) -> dict[str, Any]:
    if not item:
        return {}
    try:
        return json.loads(item.get("metadata") or "{}")
    except json.JSONDecodeError:
        return {}
