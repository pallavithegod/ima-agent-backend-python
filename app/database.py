import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator
from uuid import uuid4

from .config import get_settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    service TEXT NOT NULL,
    severity TEXT NOT NULL,
    error_category TEXT NOT NULL,
    root_cause_type TEXT NOT NULL,
    status TEXT NOT NULL,
    novelty TEXT NOT NULL,
    error_logs TEXT NOT NULL,
    diagnosis TEXT NOT NULL,
    root_cause TEXT NOT NULL,
    resolution_steps TEXT NOT NULL,
    impact TEXT NOT NULL,
    action_items TEXT NOT NULL,
    timeline TEXT NOT NULL,
    retrieved_memories TEXT NOT NULL,
    source TEXT NOT NULL,
    deployment_id TEXT,
    commit_sha TEXT,
    repository TEXT,
    git_ref TEXT,
    file_path TEXT,
    code_snippet TEXT NOT NULL DEFAULT '',
    fix_summary TEXT NOT NULL DEFAULT '',
    fix_rationale TEXT NOT NULL DEFAULT '',
    fix_diff TEXT NOT NULL DEFAULT '',
    fixed_content TEXT NOT NULL DEFAULT '',
    pull_request_url TEXT,
    remediation_error TEXT,
    engineer TEXT,
    time_to_resolve_minutes INTEGER,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deployments (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL,
    service TEXT NOT NULL,
    status TEXT NOT NULL,
    commit_sha TEXT,
    message TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_incidents_service ON incidents(service);
CREATE INDEX IF NOT EXISTS idx_incidents_created ON incidents(created_at);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
"""


JSON_FIELDS = {
    "resolution_steps",
    "action_items",
    "timeline",
    "retrieved_memories",
}


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    database = sqlite3.connect(get_settings().database_file)
    database.row_factory = sqlite3.Row
    try:
        yield database
        database.commit()
    finally:
        database.close()


def init_database() -> None:
    with connection() as database:
        database.executescript(SCHEMA)
        existing = {
            row["name"] for row in database.execute("PRAGMA table_info(incidents)").fetchall()
        }
        migrations = {
            "user_id": "TEXT NOT NULL DEFAULT ''",
            "repository": "TEXT",
            "git_ref": "TEXT",
            "file_path": "TEXT",
            "code_snippet": "TEXT NOT NULL DEFAULT ''",
            "fix_summary": "TEXT NOT NULL DEFAULT ''",
            "fix_rationale": "TEXT NOT NULL DEFAULT ''",
            "fix_diff": "TEXT NOT NULL DEFAULT ''",
            "fixed_content": "TEXT NOT NULL DEFAULT ''",
            "pull_request_url": "TEXT",
            "remediation_error": "TEXT",
        }
        for column, definition in migrations.items():
            if column not in existing:
                database.execute(f"ALTER TABLE incidents ADD COLUMN {column} {definition}")
        deployment_columns = {
            row["name"] for row in database.execute("PRAGMA table_info(deployments)").fetchall()
        }
        if "user_id" not in deployment_columns:
            database.execute("ALTER TABLE deployments ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")


def _decode_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    for field in JSON_FIELDS:
        if field in item:
            item[field] = json.loads(item[field] or "[]")
    return item


def list_incidents(
    limit: int = 100,
    status: str | None = None,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM incidents"
    params: list[Any] = []
    conditions: list[str] = []
    if user_id is not None:
        conditions.append("user_id = ?")
        params.append(user_id)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if conditions:
        query += f" WHERE {' AND '.join(conditions)}"
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with connection() as database:
        rows = database.execute(query, params).fetchall()
    return [_decode_row(row) for row in rows if row is not None]


def get_incident(incident_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    with connection() as database:
        if user_id is None:
            row = database.execute(
                "SELECT * FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
        else:
            row = database.execute(
                "SELECT * FROM incidents WHERE id = ? AND user_id = ?",
                (incident_id, user_id),
            ).fetchone()
    return _decode_row(row)


def get_incident_by_deployment(
    deployment_id: str,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    with connection() as database:
        if user_id is None:
            row = database.execute(
                "SELECT * FROM incidents WHERE deployment_id = ?", (deployment_id,)
            ).fetchone()
        else:
            row = database.execute(
                "SELECT * FROM incidents WHERE deployment_id = ? AND user_id = ?",
                (deployment_id, user_id),
            ).fetchone()
    return _decode_row(row)


def create_incident(data: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    item = {
        "id": data.get("id", str(uuid4())),
        "user_id": str(data.get("user_id", "")),
        "title": data.get("title", "Untitled incident"),
        "description": data.get("description", ""),
        "service": data.get("service", "unknown-service"),
        "severity": data.get("severity", "SEV-3"),
        "error_category": data.get("error_category", "unknown"),
        "root_cause_type": data.get("root_cause_type", "unknown"),
        "status": data.get("status", "open"),
        "novelty": data.get("novelty", "new"),
        "error_logs": data.get("error_logs", ""),
        "diagnosis": data.get("diagnosis", ""),
        "root_cause": data.get("root_cause", ""),
        "resolution_steps": data.get("resolution_steps", []),
        "impact": data.get("impact", "Under investigation"),
        "action_items": data.get("action_items", []),
        "timeline": data.get("timeline", []),
        "retrieved_memories": data.get("retrieved_memories", []),
        "source": data.get("source", "manual"),
        "deployment_id": data.get("deployment_id"),
        "commit_sha": data.get("commit_sha"),
        "repository": data.get("repository"),
        "git_ref": data.get("git_ref"),
        "file_path": data.get("file_path"),
        "code_snippet": data.get("code_snippet", ""),
        "fix_summary": data.get("fix_summary", ""),
        "fix_rationale": data.get("fix_rationale", ""),
        "fix_diff": data.get("fix_diff", ""),
        "fixed_content": data.get("fixed_content", ""),
        "pull_request_url": data.get("pull_request_url"),
        "remediation_error": data.get("remediation_error"),
        "engineer": data.get("engineer"),
        "time_to_resolve_minutes": data.get("time_to_resolve_minutes"),
        "created_at": data.get("created_at", now),
        "resolved_at": data.get("resolved_at"),
        "updated_at": now,
    }
    columns = list(item)
    values = [
        json.dumps(item[column]) if column in JSON_FIELDS else item[column]
        for column in columns
    ]
    placeholders = ", ".join("?" for _ in columns)
    with connection() as database:
        database.execute(
            f"INSERT INTO incidents ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
    return get_incident(item["id"]) or item


def update_incident(incident_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
    allowed = {
        "status",
        "diagnosis",
        "root_cause",
        "resolution_steps",
        "impact",
        "action_items",
        "timeline",
        "engineer",
        "time_to_resolve_minutes",
        "resolved_at",
        "file_path",
        "code_snippet",
        "fix_summary",
        "fix_rationale",
        "fix_diff",
        "fixed_content",
        "pull_request_url",
        "remediation_error",
    }
    clean = {key: value for key, value in changes.items() if key in allowed}
    if not clean:
        return get_incident(incident_id)
    clean["updated_at"] = datetime.now(UTC).isoformat()
    assignments = ", ".join(f"{key} = ?" for key in clean)
    values = [
        json.dumps(value) if key in JSON_FIELDS else value
        for key, value in clean.items()
    ]
    with connection() as database:
        database.execute(
            f"UPDATE incidents SET {assignments} WHERE id = ?",
            [*values, incident_id],
        )
    return get_incident(incident_id)


def create_deployment(data: dict[str, Any]) -> dict[str, Any]:
    item = {
        "id": data.get("id") or str(uuid4()),
        "user_id": str(data.get("user_id", "")),
        "platform": data.get("platform", "render"),
        "service": data.get("service", "unknown-service"),
        "status": data.get("status", "unknown"),
        "commit_sha": data.get("commit_sha"),
        "message": data.get("message", ""),
        "raw_payload": json.dumps(data.get("raw_payload", {})),
        "created_at": data.get("created_at", datetime.now(UTC).isoformat()),
    }
    with connection() as database:
        database.execute(
            """
            INSERT OR REPLACE INTO deployments
            (id, user_id, platform, service, status, commit_sha, message, raw_payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(item.values()),
        )
    item["raw_payload"] = json.loads(item["raw_payload"])
    return item


def list_deployments(limit: int = 50, user_id: str | None = None) -> list[dict[str, Any]]:
    with connection() as database:
        if user_id is None:
            rows = database.execute(
                "SELECT * FROM deployments ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = database.execute(
                "SELECT * FROM deployments WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
    items = [dict(row) for row in rows]
    for item in items:
        item["raw_payload"] = json.loads(item["raw_payload"] or "{}")
    return items


def get_deployment(deployment_id: str) -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            "SELECT * FROM deployments WHERE id = ?", (deployment_id,)
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["raw_payload"] = json.loads(item["raw_payload"] or "{}")
    return item
