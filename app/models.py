from typing import Literal

from pydantic import BaseModel, Field


class IncidentCreate(BaseModel):
    title: str = Field(min_length=3, max_length=140)
    description: str = Field(min_length=5)
    error_logs: str = ""
    service_hint: str | None = None
    severity_hint: str | None = None
    source: Literal["manual", "vercel", "render", "github", "api"] = "manual"
    deployment_id: str | None = None
    commit_sha: str | None = None


class IncidentResolve(BaseModel):
    resolution_steps: list[str] = Field(min_length=1)
    root_cause: str = Field(min_length=3)
    engineer: str = "On-call engineer"
    impact: str | None = None
    time_to_resolve_minutes: int | None = Field(default=None, ge=0)


class RepairApproval(BaseModel):
    approved: bool


class HealthResponse(BaseModel):
    status: str
    llm_mode: str
    memory_mode: str
