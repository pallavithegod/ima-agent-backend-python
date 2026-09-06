from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

# JSONB on Postgres, plain JSON elsewhere so unit tests can run on SQLite.
JsonColumn = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    firebase_uid: Mapped[str | None] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="engineer")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class ProviderConnection(Base):
    __tablename__ = "provider_connections"
    __table_args__ = (UniqueConstraint("user_id", "provider"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[str | None] = mapped_column(Text)
    account_id: Mapped[str | None] = mapped_column(Text)
    account_name: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column("metadata", Text, nullable=False, default="{}")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class TrackedProject(Base):
    __tablename__ = "tracked_projects"
    __table_args__ = (UniqueConstraint("user_id", "vercel_project_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    vercel_project_id: Mapped[str] = mapped_column(Text, nullable=False)
    vercel_project_name: Mapped[str] = mapped_column(Text, nullable=False)
    vercel_team_id: Mapped[str | None] = mapped_column(Text)
    github_repository: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class RenderServiceMapping(Base):
    __tablename__ = "render_services"
    __table_args__ = (UniqueConstraint("user_id", "render_service_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    render_service_id: Mapped[str] = mapped_column(Text, nullable=False)
    render_service_name: Mapped[str] = mapped_column(Text, nullable=False)
    render_owner_id: Mapped[str | None] = mapped_column(Text)
    github_repository: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class MonitoredRepository(Base):
    __tablename__ = "monitored_repositories"
    __table_args__ = (UniqueConstraint("user_id", "github_repository"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    github_repository: Mapped[str] = mapped_column(Text, nullable=False)
    repository_id: Mapped[str | None] = mapped_column(Text)
    private: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    default_branch: Mapped[str | None] = mapped_column(Text)
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    last_commit_sha: Mapped[str | None] = mapped_column(Text)
    last_commit_at: Mapped[str | None] = mapped_column(Text)
    github_etag: Mapped[str | None] = mapped_column(Text)
    last_commit_checked_at: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[str | None] = mapped_column(Text)
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class CommitWorkflow(Base):
    __tablename__ = "commit_workflows"
    __table_args__ = (UniqueConstraint("user_id", "github_repository", "commit_sha"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    github_repository: Mapped[str] = mapped_column(Text, nullable=False)
    commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    commit_message: Mapped[str | None] = mapped_column(Text)
    commit_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="commit_detected")
    deployment_id: Mapped[str | None] = mapped_column(Text)
    incident_id: Mapped[str | None] = mapped_column(Text)
    pull_request_url: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class OAuthState(Base):
    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    code_verifier: Mapped[str | None] = mapped_column(Text)
    nonce: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        Index("idx_incidents_service", "service"),
        Index("idx_incidents_created", "created_at"),
        Index("idx_incidents_status", "status"),
        Index("idx_incidents_user_created", "user_id", "created_at"),
        Index("idx_incidents_deployment", "deployment_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    service: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    error_category: Mapped[str] = mapped_column(Text, nullable=False)
    root_cause_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    novelty: Mapped[str] = mapped_column(Text, nullable=False)
    error_logs: Mapped[str] = mapped_column(Text, nullable=False)
    diagnosis: Mapped[str] = mapped_column(Text, nullable=False)
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_steps: Mapped[list] = mapped_column(JsonColumn, nullable=False, default=list)
    impact: Mapped[str] = mapped_column(Text, nullable=False)
    action_items: Mapped[list] = mapped_column(JsonColumn, nullable=False, default=list)
    timeline: Mapped[list] = mapped_column(JsonColumn, nullable=False, default=list)
    retrieved_memories: Mapped[list] = mapped_column(JsonColumn, nullable=False, default=list)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_id: Mapped[str | None] = mapped_column(Text)
    commit_sha: Mapped[str | None] = mapped_column(Text)
    repository: Mapped[str | None] = mapped_column(Text)
    git_ref: Mapped[str | None] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(Text)
    code_snippet: Mapped[str] = mapped_column(Text, nullable=False, default="")
    fix_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    fix_rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    fix_diff: Mapped[str] = mapped_column(Text, nullable=False, default="")
    fixed_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    pull_request_url: Mapped[str | None] = mapped_column(Text)
    remediation_error: Mapped[str | None] = mapped_column(Text)
    engineer: Mapped[str | None] = mapped_column(Text)
    time_to_resolve_minutes: Mapped[int | None] = mapped_column(Integer)
    vm_id: Mapped[int | None] = mapped_column(ForeignKey("monitored_vms.id", ondelete="SET NULL"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_at: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    service: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    commit_sha: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JsonColumn, nullable=False, default=dict)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class MonitoredVM(Base):
    __tablename__ = "monitored_vms"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    service_name: Mapped[str] = mapped_column(Text, nullable=False)
    repository: Mapped[str] = mapped_column(Text, nullable=False)
    default_branch: Mapped[str] = mapped_column(Text, nullable=False, default="main")
    api_key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="offline")
    last_heartbeat_at: Mapped[str | None] = mapped_column(Text)
    last_event_at: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class VMEvent(Base):
    __tablename__ = "vm_events"
    __table_args__ = (Index("idx_vm_events_dedup", "vm_id", "signature", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vm_id: Mapped[int] = mapped_column(ForeignKey("monitored_vms.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False, default="")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    log_excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JsonColumn, nullable=False, default=dict)
    incident_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
