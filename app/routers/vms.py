"""VM registry and ingest endpoints. Project VMs authenticate with a per-VM
API key (X-VM-Api-Key header); registry management uses the user JWT."""

import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from ..db import repo
from ..security import current_user
from ..services import vm_monitor
from ..services.crypto import hash_password, verify_password

router = APIRouter(prefix="/api/vms", tags=["vms"])


def _public_vm(vm: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in vm.items() if key != "api_key_hash"}


def _authorized_vm(vm_id: int, api_key: str | None) -> dict[str, Any]:
    vm = repo.get_vm(vm_id)
    if not vm or not api_key or not verify_password(api_key, vm["api_key_hash"]):
        raise HTTPException(status_code=401, detail="Invalid VM credentials")
    if not vm.get("enabled"):
        raise HTTPException(status_code=403, detail="VM is disabled")
    return vm


class VMCreatePayload(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    serviceName: str = Field(min_length=2, max_length=120)
    repository: str = Field(pattern=r"^[^/]+/[^/]+$")
    defaultBranch: str = "main"


class VMUpdatePayload(BaseModel):
    serviceName: str | None = None
    repository: str | None = Field(default=None, pattern=r"^[^/]+/[^/]+$")
    defaultBranch: str | None = None
    enabled: bool | None = None


class HeartbeatPayload(BaseModel):
    status: str = "online"
    appPid: int | None = None
    uptimeSeconds: float | None = None


class VMEventPayload(BaseModel):
    type: str = Field(pattern="^(crash|health_check_failed|log_error)$")
    signature: str = ""
    message: str = ""
    exitCode: int | None = None
    logExcerpt: str = Field(default="", max_length=200_000)
    occurredAt: str | None = None


@router.post("", status_code=201)
def register_vm(
    payload: VMCreatePayload, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    if repo.get_vm_by_name(user["sub"], payload.name):
        raise HTTPException(status_code=409, detail="A VM with this name is already registered")
    api_key = secrets.token_urlsafe(32)
    vm = repo.create_vm(
        user["sub"],
        payload.name,
        payload.serviceName,
        payload.repository,
        payload.defaultBranch or "main",
        hash_password(api_key),
    )
    # The key is returned exactly once; only its hash is stored.
    return {"vm": _public_vm(vm), "apiKey": api_key}


@router.get("")
def list_vms(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return {"vms": [_public_vm(vm) for vm in repo.list_vms(user["sub"])]}


@router.patch("/{vm_id}")
def update_vm(
    vm_id: int, payload: VMUpdatePayload, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    vm = repo.get_vm(vm_id)
    if not vm or str(vm["user_id"]) != str(user["sub"]):
        raise HTTPException(status_code=404, detail="VM not found")
    changes = {
        "service_name": payload.serviceName,
        "repository": payload.repository,
        "default_branch": payload.defaultBranch,
        "enabled": payload.enabled,
    }
    updated = repo.update_vm(vm_id, {k: v for k, v in changes.items() if v is not None})
    return {"vm": _public_vm(updated)}


@router.delete("/{vm_id}", status_code=204)
def delete_vm(vm_id: int, user: dict[str, Any] = Depends(current_user)) -> None:
    repo.delete_vm(user["sub"], vm_id)


@router.post("/{vm_id}/heartbeat")
def heartbeat(
    vm_id: int,
    payload: HeartbeatPayload,
    x_vm_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    vm = _authorized_vm(vm_id, x_vm_api_key)
    repo.update_vm(
        vm["id"],
        {
            "last_heartbeat_at": repo.now_iso(),
            "status": payload.status if payload.status in ("online", "degraded") else "online",
        },
    )
    return {"ok": True}


@router.post("/{vm_id}/events", status_code=201)
async def ingest_event(
    vm_id: int,
    payload: VMEventPayload,
    x_vm_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    vm = _authorized_vm(vm_id, x_vm_api_key)
    event = payload.model_dump()
    if vm_monitor.is_duplicate(vm, event.get("signature") or ""):
        return {"deduplicated": True}
    result = await vm_monitor.process_event(vm, event)
    incident = result["incident"]
    return {
        "deduplicated": False,
        "incidentId": incident["id"],
        "pullRequestUrl": incident.get("pull_request_url"),
        "pullRequestError": result["pull_request_error"],
    }
