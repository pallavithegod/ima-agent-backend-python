"""Data access layer. Preserves the dict shapes the services and API returned
when the backend used raw sqlite3, so route payloads stay unchanged."""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy import inspect as sa_inspect

from .models import (
    Base,
    CommitWorkflow,
    Deployment,
    Incident,
    MonitoredRepository,
    MonitoredVM,
    OAuthState,
    ProviderConnection,
    RenderServiceMapping,
    TrackedProject,
    User,
    VMEvent,
)
from .session import get_engine, session_scope


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def init_database() -> None:
    Base.metadata.create_all(get_engine())


def _row(obj: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    data = {}
    for attr in sa_inspect(obj).mapper.column_attrs:
        data[attr.columns[0].name] = getattr(obj, attr.key)
    return data


# ---------------------------------------------------------------------------
# Incidents


def list_incidents(
    limit: int = 100,
    status: str | None = None,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    with session_scope() as session:
        query = select(Incident)
        if user_id is not None:
            query = query.where(Incident.user_id == user_id)
        if status:
            query = query.where(Incident.status == status)
        query = query.order_by(Incident.created_at.desc()).limit(limit)
        return [_row(item) for item in session.scalars(query)]


def get_incident(incident_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    with session_scope() as session:
        query = select(Incident).where(Incident.id == incident_id)
        if user_id is not None:
            query = query.where(Incident.user_id == user_id)
        return _row(session.scalars(query).first())


def get_incident_by_deployment(
    deployment_id: str,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    with session_scope() as session:
        query = select(Incident).where(Incident.deployment_id == deployment_id)
        if user_id is not None:
            query = query.where(Incident.user_id == user_id)
        return _row(session.scalars(query).first())


def _coerce_text(value: Any, default: str = "") -> str:
    """LLM outputs sometimes nest objects where the schema expects prose."""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _coerce_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def create_incident(data: dict[str, Any]) -> dict[str, Any]:
    now = now_iso()
    data = {
        **data,
        **{
            key: _coerce_text(data.get(key))
            for key in (
                "title",
                "description",
                "diagnosis",
                "root_cause",
                "impact",
                "service",
                "severity",
                "error_category",
                "root_cause_type",
            )
            if key in data
        },
        **{
            key: _coerce_list(data.get(key))
            for key in ("resolution_steps", "action_items", "timeline", "retrieved_memories")
            if key in data
        },
    }
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
        "vm_id": data.get("vm_id"),
        "created_at": data.get("created_at") or now,
        "resolved_at": data.get("resolved_at"),
        "updated_at": now,
    }
    with session_scope() as session:
        session.add(Incident(**item))
    return get_incident(item["id"]) or item


INCIDENT_UPDATE_FIELDS = {
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


def update_incident(incident_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
    clean = {key: value for key, value in changes.items() if key in INCIDENT_UPDATE_FIELDS}
    if not clean:
        return get_incident(incident_id)
    clean["updated_at"] = now_iso()
    with session_scope() as session:
        session.execute(update(Incident).where(Incident.id == incident_id).values(**clean))
    return get_incident(incident_id)


# ---------------------------------------------------------------------------
# Deployments


def create_deployment(data: dict[str, Any]) -> dict[str, Any]:
    item = {
        "id": data.get("id") or str(uuid4()),
        "user_id": str(data.get("user_id", "")),
        "platform": data.get("platform", "render"),
        "service": data.get("service", "unknown-service"),
        "status": data.get("status", "unknown"),
        "commit_sha": data.get("commit_sha"),
        "message": data.get("message", ""),
        "raw_payload": data.get("raw_payload", {}),
        "created_at": str(data.get("created_at") or now_iso()),
    }
    with session_scope() as session:
        existing = session.get(Deployment, item["id"])
        if existing:
            for key, value in item.items():
                setattr(existing, key, value)
        else:
            session.add(Deployment(**item))
    return item


def list_deployments(limit: int = 50, user_id: str | None = None) -> list[dict[str, Any]]:
    with session_scope() as session:
        query = select(Deployment)
        if user_id is not None:
            query = query.where(Deployment.user_id == user_id)
        query = query.order_by(Deployment.created_at.desc()).limit(limit)
        return [_row(item) for item in session.scalars(query)]


def get_deployment(deployment_id: str) -> dict[str, Any] | None:
    with session_scope() as session:
        return _row(session.get(Deployment, deployment_id))


# ---------------------------------------------------------------------------
# Users


def safe_user(user: dict[str, Any] | None) -> dict[str, Any] | None:
    if not user:
        return None
    return {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"],
        "role": user["role"],
        "createdAt": user["created_at"],
    }


def get_user_by_email(email: str) -> dict[str, Any] | None:
    with session_scope() as session:
        return _row(session.scalars(select(User).where(User.email == email.lower())).first())


def get_user_by_id(user_id: int | str) -> dict[str, Any] | None:
    with session_scope() as session:
        return _row(session.get(User, int(user_id)))


def create_user(name: str, email: str, password_hash: str, role: str = "engineer") -> dict[str, Any]:
    with session_scope() as session:
        user = User(
            name=name,
            email=email.lower(),
            password_hash=password_hash,
            role=role,
            created_at=now_iso(),
        )
        session.add(user)
        session.flush()
        return _row(user)


def upsert_firebase_user(name: str, email: str, firebase_uid: str) -> dict[str, Any]:
    with session_scope() as session:
        user = session.scalars(select(User).where(User.email == email.lower())).first()
        if user:
            user.name = name
            user.firebase_uid = firebase_uid
        else:
            user = User(
                name=name,
                email=email.lower(),
                firebase_uid=firebase_uid,
                password_hash="firebase-managed",
                role="engineer",
                created_at=now_iso(),
            )
            session.add(user)
        session.flush()
        return _row(user)


# ---------------------------------------------------------------------------
# Provider connections (values arrive already encrypted)


def get_connection(user_id: int | str, provider: str) -> dict[str, Any] | None:
    with session_scope() as session:
        return _row(
            session.scalars(
                select(ProviderConnection).where(
                    ProviderConnection.user_id == int(user_id),
                    ProviderConnection.provider == provider,
                )
            ).first()
        )


def list_connections(user_id: int | str) -> list[dict[str, Any]]:
    with session_scope() as session:
        return [
            _row(item)
            for item in session.scalars(
                select(ProviderConnection).where(ProviderConnection.user_id == int(user_id))
            )
        ]


def upsert_connection_row(
    user_id: int | str,
    provider: str,
    encrypted_access_token: str,
    account_id: str | None,
    account_name: str | None,
    metadata: dict[str, Any] | None = None,
    encrypted_refresh_token: str | None = None,
    token_expires_at: str | None = None,
) -> None:
    now = now_iso()
    with session_scope() as session:
        item = session.scalars(
            select(ProviderConnection).where(
                ProviderConnection.user_id == int(user_id),
                ProviderConnection.provider == provider,
            )
        ).first()
        if item:
            item.access_token = encrypted_access_token
            item.refresh_token = encrypted_refresh_token
            item.token_expires_at = token_expires_at
            item.account_id = account_id
            item.account_name = account_name
            item.metadata_json = json.dumps(metadata or {})
            item.updated_at = now
        else:
            session.add(
                ProviderConnection(
                    user_id=int(user_id),
                    provider=provider,
                    access_token=encrypted_access_token,
                    refresh_token=encrypted_refresh_token,
                    token_expires_at=token_expires_at,
                    account_id=account_id,
                    account_name=account_name,
                    metadata_json=json.dumps(metadata or {}),
                    created_at=now,
                    updated_at=now,
                )
            )


def clear_connection_refresh(user_id: int | str, provider: str) -> None:
    with session_scope() as session:
        session.execute(
            update(ProviderConnection)
            .where(
                ProviderConnection.user_id == int(user_id),
                ProviderConnection.provider == provider,
            )
            .values(refresh_token=None, token_expires_at=None, updated_at=now_iso())
        )


def delete_connection(user_id: int | str, provider: str) -> None:
    with session_scope() as session:
        session.execute(
            delete(ProviderConnection).where(
                ProviderConnection.user_id == int(user_id),
                ProviderConnection.provider == provider,
            )
        )


# ---------------------------------------------------------------------------
# Tracked Vercel projects


def list_tracked_projects(user_id: int | str, enabled_only: bool = False) -> list[dict[str, Any]]:
    with session_scope() as session:
        query = select(TrackedProject).where(TrackedProject.user_id == int(user_id))
        if enabled_only:
            query = query.where(TrackedProject.enabled == 1)
        query = query.order_by(TrackedProject.created_at.desc())
        return [_row(item) for item in session.scalars(query)]


def upsert_tracked_project(
    user_id: int | str,
    vercel_project_id: str,
    vercel_project_name: str,
    vercel_team_id: str | None,
    github_repository: str,
) -> None:
    now = now_iso()
    with session_scope() as session:
        item = session.scalars(
            select(TrackedProject).where(
                TrackedProject.user_id == int(user_id),
                TrackedProject.vercel_project_id == vercel_project_id,
            )
        ).first()
        if item:
            item.vercel_project_name = vercel_project_name
            item.vercel_team_id = vercel_team_id
            item.github_repository = github_repository
            item.enabled = 1
            item.updated_at = now
        else:
            session.add(
                TrackedProject(
                    user_id=int(user_id),
                    vercel_project_id=vercel_project_id,
                    vercel_project_name=vercel_project_name,
                    vercel_team_id=vercel_team_id,
                    github_repository=github_repository,
                    created_at=now,
                    updated_at=now,
                )
            )


def get_tracked_project_by_repo(user_id: int | str, github_repository: str) -> dict[str, Any] | None:
    with session_scope() as session:
        return _row(
            session.scalars(
                select(TrackedProject).where(
                    TrackedProject.user_id == int(user_id),
                    func.lower(TrackedProject.github_repository) == github_repository.lower(),
                    TrackedProject.enabled == 1,
                )
            ).first()
        )


def delete_tracked_project(user_id: int | str, project_id: int | str) -> None:
    with session_scope() as session:
        session.execute(
            delete(TrackedProject).where(
                TrackedProject.id == int(project_id),
                TrackedProject.user_id == int(user_id),
            )
        )


# ---------------------------------------------------------------------------
# Render service mappings


def list_render_service_rows(user_id: int | str, enabled_only: bool = False) -> list[dict[str, Any]]:
    with session_scope() as session:
        query = select(RenderServiceMapping).where(RenderServiceMapping.user_id == int(user_id))
        if enabled_only:
            query = query.where(RenderServiceMapping.enabled == 1)
        query = query.order_by(RenderServiceMapping.created_at.desc())
        return [_row(item) for item in session.scalars(query)]


def upsert_render_service(
    user_id: int | str,
    render_service_id: str,
    render_service_name: str,
    render_owner_id: str | None,
    github_repository: str,
) -> None:
    now = now_iso()
    with session_scope() as session:
        item = session.scalars(
            select(RenderServiceMapping).where(
                RenderServiceMapping.user_id == int(user_id),
                RenderServiceMapping.render_service_id == render_service_id,
            )
        ).first()
        if item:
            item.render_service_name = render_service_name
            item.render_owner_id = render_owner_id
            item.github_repository = github_repository
            item.enabled = 1
            item.updated_at = now
        else:
            session.add(
                RenderServiceMapping(
                    user_id=int(user_id),
                    render_service_id=render_service_id,
                    render_service_name=render_service_name,
                    render_owner_id=render_owner_id,
                    github_repository=github_repository,
                    created_at=now,
                    updated_at=now,
                )
            )


# ---------------------------------------------------------------------------
# Monitored repositories


def list_monitored_repositories(user_id: int | str | None = None) -> list[dict[str, Any]]:
    with session_scope() as session:
        query = select(MonitoredRepository)
        if user_id is not None:
            query = query.where(MonitoredRepository.user_id == int(user_id))
        query = query.order_by(MonitoredRepository.created_at.desc())
        return [_row(item) for item in session.scalars(query)]


def get_monitored_repository(user_id: int | str, github_repository: str) -> dict[str, Any] | None:
    with session_scope() as session:
        return _row(
            session.scalars(
                select(MonitoredRepository).where(
                    MonitoredRepository.user_id == int(user_id),
                    func.lower(MonitoredRepository.github_repository)
                    == github_repository.lower(),
                )
            ).first()
        )


def upsert_monitored_repository(
    user_id: int | str,
    github_repository: str,
    repository_id: str | None,
    private: bool,
    default_branch: str | None,
) -> None:
    now = now_iso()
    with session_scope() as session:
        item = session.scalars(
            select(MonitoredRepository).where(
                MonitoredRepository.user_id == int(user_id),
                MonitoredRepository.github_repository == github_repository,
            )
        ).first()
        if item:
            item.repository_id = repository_id
            item.private = 1 if private else 0
            item.default_branch = default_branch
            item.updated_at = now
        else:
            session.add(
                MonitoredRepository(
                    user_id=int(user_id),
                    github_repository=github_repository,
                    repository_id=repository_id,
                    private=1 if private else 0,
                    default_branch=default_branch,
                    created_at=now,
                    updated_at=now,
                )
            )


def update_repository_settings(
    user_id: int | str, github_repository: str, poll_interval_seconds: int
) -> bool:
    with session_scope() as session:
        result = session.execute(
            update(MonitoredRepository)
            .where(
                MonitoredRepository.user_id == int(user_id),
                func.lower(MonitoredRepository.github_repository) == github_repository.lower(),
            )
            .values(poll_interval_seconds=poll_interval_seconds, updated_at=now_iso())
        )
        return result.rowcount > 0


def update_monitored_repository(repo_id: int, changes: dict[str, Any]) -> None:
    changes = {**changes, "updated_at": now_iso()}
    with session_scope() as session:
        session.execute(
            update(MonitoredRepository).where(MonitoredRepository.id == repo_id).values(**changes)
        )


def update_repositories_synced(user_id: int | str, error: str | None) -> None:
    now = now_iso()
    with session_scope() as session:
        session.execute(
            update(MonitoredRepository)
            .where(MonitoredRepository.user_id == int(user_id))
            .values(last_synced_at=now, last_sync_error=error, updated_at=now)
        )


# ---------------------------------------------------------------------------
# Commit workflows


def insert_commit_workflow(
    user_id: int | str,
    github_repository: str,
    commit_sha: str,
    commit_message: str | None,
    commit_url: str | None,
) -> None:
    now = now_iso()
    with session_scope() as session:
        existing = session.scalars(
            select(CommitWorkflow).where(
                CommitWorkflow.user_id == int(user_id),
                CommitWorkflow.github_repository == github_repository,
                CommitWorkflow.commit_sha == commit_sha,
            )
        ).first()
        if existing:
            return
        session.add(
            CommitWorkflow(
                user_id=int(user_id),
                github_repository=github_repository,
                commit_sha=commit_sha,
                commit_message=commit_message,
                commit_url=commit_url,
                status="commit_detected",
                created_at=now,
                updated_at=now,
            )
        )


def update_commit_workflow(
    user_id: int | str,
    github_repository: str,
    commit_sha: str,
    changes: dict[str, Any],
) -> None:
    changes = {**changes, "updated_at": now_iso()}
    with session_scope() as session:
        session.execute(
            update(CommitWorkflow)
            .where(
                CommitWorkflow.user_id == int(user_id),
                func.lower(CommitWorkflow.github_repository) == github_repository.lower(),
                CommitWorkflow.commit_sha == commit_sha,
            )
            .values(**changes)
        )


def list_commit_workflows(
    user_id: int | str, github_repository: str, limit: int = 20
) -> list[dict[str, Any]]:
    with session_scope() as session:
        query = (
            select(CommitWorkflow)
            .where(
                CommitWorkflow.user_id == int(user_id),
                func.lower(CommitWorkflow.github_repository) == github_repository.lower(),
            )
            .order_by(CommitWorkflow.created_at.desc())
            .limit(limit)
        )
        return [_row(item) for item in session.scalars(query)]


# ---------------------------------------------------------------------------
# OAuth states


def create_oauth_state(
    state: str,
    user_id: int | str,
    provider: str,
    code_verifier: str | None,
    nonce: str | None,
    ttl_minutes: int = 10,
) -> None:
    now = datetime.now(UTC)
    with session_scope() as session:
        session.add(
            OAuthState(
                state=state,
                user_id=int(user_id),
                provider=provider,
                code_verifier=code_verifier,
                nonce=nonce,
                expires_at=datetime.fromtimestamp(
                    now.timestamp() + ttl_minutes * 60, UTC
                ).isoformat(),
                created_at=now.isoformat(),
            )
        )


def consume_oauth_state(state: str, provider: str) -> dict[str, Any] | None:
    with session_scope() as session:
        item = session.scalars(
            select(OAuthState).where(OAuthState.state == state, OAuthState.provider == provider)
        ).first()
        data = _row(item)
        if item:
            session.delete(item)
    if not data:
        return None
    if datetime.fromisoformat(data["expires_at"]) < datetime.now(UTC):
        return None
    return data


# ---------------------------------------------------------------------------
# Monitor queries


def repositories_with_github_connection() -> list[dict[str, Any]]:
    """Monitored repos joined with their owner and GitHub connection."""
    with session_scope() as session:
        rows = session.execute(
            select(MonitoredRepository, User, ProviderConnection.access_token)
            .join(User, User.id == MonitoredRepository.user_id)
            .join(
                ProviderConnection,
                (ProviderConnection.user_id == MonitoredRepository.user_id)
                & (ProviderConnection.provider == "github"),
            )
        ).all()
        results = []
        for repo, user, access_token in rows:
            item = _row(repo)
            item["user"] = _row(user)
            item["github_access_token"] = access_token
            results.append(item)
        return results


def users_with_tracked_resources() -> list[dict[str, Any]]:
    """Users with enabled tracked projects/services, plus their min poll interval."""
    with session_scope() as session:
        rows = session.execute(
            select(
                User,
                func.min(MonitoredRepository.poll_interval_seconds),
                func.max(MonitoredRepository.last_synced_at),
            )
            .join(MonitoredRepository, MonitoredRepository.user_id == User.id)
            .join(TrackedProject, TrackedProject.user_id == User.id)
            .where(TrackedProject.enabled == 1)
            .group_by(User.id)
        ).all()
        results = []
        for user, poll_interval, last_synced in rows:
            item = _row(user)
            item["poll_interval_seconds"] = poll_interval
            item["last_synced_at"] = last_synced
            results.append(item)
        return results


def user_has_enabled_provider(user_id: int | str, provider: str) -> bool:
    table = TrackedProject if provider == "vercel" else RenderServiceMapping
    with session_scope() as session:
        connected = session.scalars(
            select(ProviderConnection.id).where(
                ProviderConnection.user_id == int(user_id),
                ProviderConnection.provider == provider,
            )
        ).first()
        if not connected:
            return False
        return (
            session.scalars(
                select(table.id).where(table.user_id == int(user_id), table.enabled == 1)
            ).first()
            is not None
        )


# ---------------------------------------------------------------------------
# Monitored VMs & events


def create_vm(
    user_id: int | str,
    name: str,
    service_name: str,
    repository: str,
    default_branch: str,
    api_key_hash: str,
) -> dict[str, Any]:
    now = now_iso()
    with session_scope() as session:
        vm = MonitoredVM(
            user_id=int(user_id),
            name=name,
            service_name=service_name,
            repository=repository,
            default_branch=default_branch,
            api_key_hash=api_key_hash,
            created_at=now,
            updated_at=now,
        )
        session.add(vm)
        session.flush()
        return _row(vm)


def list_vms(user_id: int | str) -> list[dict[str, Any]]:
    with session_scope() as session:
        return [
            _row(item)
            for item in session.scalars(
                select(MonitoredVM)
                .where(MonitoredVM.user_id == int(user_id))
                .order_by(MonitoredVM.created_at.desc())
            )
        ]


def get_vm(vm_id: int | str) -> dict[str, Any] | None:
    with session_scope() as session:
        return _row(session.get(MonitoredVM, int(vm_id)))


def get_vm_by_name(user_id: int | str, name: str) -> dict[str, Any] | None:
    with session_scope() as session:
        return _row(
            session.scalars(
                select(MonitoredVM).where(
                    MonitoredVM.user_id == int(user_id), MonitoredVM.name == name
                )
            ).first()
        )


def update_vm(vm_id: int | str, changes: dict[str, Any]) -> dict[str, Any] | None:
    allowed = {
        "name",
        "service_name",
        "repository",
        "default_branch",
        "api_key_hash",
        "status",
        "last_heartbeat_at",
        "last_event_at",
        "enabled",
    }
    clean = {key: value for key, value in changes.items() if key in allowed}
    if clean:
        clean["updated_at"] = now_iso()
        with session_scope() as session:
            session.execute(
                update(MonitoredVM).where(MonitoredVM.id == int(vm_id)).values(**clean)
            )
    return get_vm(vm_id)


def delete_vm(user_id: int | str, vm_id: int | str) -> None:
    with session_scope() as session:
        session.execute(
            delete(MonitoredVM).where(
                MonitoredVM.id == int(vm_id), MonitoredVM.user_id == int(user_id)
            )
        )


def list_all_enabled_vms() -> list[dict[str, Any]]:
    with session_scope() as session:
        return [
            _row(item)
            for item in session.scalars(select(MonitoredVM).where(MonitoredVM.enabled.is_(True)))
        ]


def create_vm_event(
    vm_id: int | str,
    event_type: str,
    signature: str,
    message: str,
    log_excerpt: str,
    payload: dict[str, Any] | None = None,
    incident_id: str | None = None,
) -> dict[str, Any]:
    with session_scope() as session:
        event = VMEvent(
            vm_id=int(vm_id),
            event_type=event_type,
            signature=signature,
            message=message,
            log_excerpt=log_excerpt,
            payload=payload or {},
            incident_id=incident_id,
            created_at=now_iso(),
        )
        session.add(event)
        session.flush()
        return _row(event)


def update_vm_event(event_id: int, incident_id: str | None) -> None:
    with session_scope() as session:
        session.execute(
            update(VMEvent).where(VMEvent.id == int(event_id)).values(incident_id=incident_id)
        )


def latest_vm_event_by_signature(vm_id: int | str, signature: str) -> dict[str, Any] | None:
    with session_scope() as session:
        return _row(
            session.scalars(
                select(VMEvent)
                .where(VMEvent.vm_id == int(vm_id), VMEvent.signature == signature)
                .order_by(VMEvent.created_at.desc())
            ).first()
        )
