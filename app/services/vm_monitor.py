"""VM event → incident pipeline and heartbeat staleness sweeper.

VM incidents flow through the same LangGraph analysis and clone-based fixer as
deployment failures, and surface in the existing incidents/analytics UI."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from ..agent import analyze_incident
from ..config import get_settings
from ..db import repo
from .fixer import fixer_service
from .memory import memory_service
from .runtime import get_runtime

logger = logging.getLogger("recallops.vm")

SWEEP_SECONDS = 30

EVENT_SEVERITY = {
    "crash": "SEV-1",
    "health_check_failed": "SEV-2",
    "log_error": "SEV-3",
    "heartbeat_lost": "SEV-2",
}


def is_duplicate(vm: dict[str, Any], signature: str) -> bool:
    if not signature:
        return False
    settings = get_settings()
    previous = repo.latest_vm_event_by_signature(vm["id"], signature)
    if not previous:
        return False
    age = (
        datetime.now(UTC) - datetime.fromisoformat(previous["created_at"])
    ).total_seconds()
    if age < settings.vm_event_cooldown_seconds:
        return True
    if previous.get("incident_id"):
        incident = repo.get_incident(previous["incident_id"])
        if incident and incident.get("status") == "open":
            return True
    return False


async def process_event(vm: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """Create the incident, run diagnosis, and attempt an automated fix PR."""
    event_type = event["type"]
    message = event.get("message") or f"{vm['service_name']} reported {event_type}"
    logs = event.get("logExcerpt") or ""
    stored_event = repo.create_vm_event(
        vm["id"], event_type, event.get("signature") or "", message, logs,
        {key: value for key, value in event.items() if key not in ("logExcerpt",)},
    )
    repo.update_vm(vm["id"], {"last_event_at": repo.now_iso(), "status": "degraded"})

    incident = await asyncio.to_thread(
        analyze_incident,
        {
            "title": f"{vm['service_name']} {event_type.replace('_', ' ')} on {vm['name']}",
            "user_id": str(vm["user_id"]),
            "description": message,
            "error_logs": logs,
            "service_hint": vm["service_name"],
            "severity_hint": EVENT_SEVERITY.get(event_type),
            "source": "vm",
            "repository": vm["repository"],
            "git_ref": vm.get("default_branch") or "main",
            "vm_id": vm["id"],
        },
    )
    repo.update_vm_event(stored_event["id"], incident["id"])
    # A synthetic deployment row makes the crash visible on the deployments panel.
    repo.create_deployment(
        {
            "user_id": str(vm["user_id"]),
            "platform": "vm",
            "service": vm["service_name"],
            "status": event_type.upper(),
            "message": message,
            "raw_payload": {"vm": vm["name"], "event": event_type, "logs": logs[-5000:]},
        }
    )

    pull_request_error = None
    try:
        runtime = await get_runtime(vm["user_id"])
        incident = await fixer_service.fix_and_pr(incident, runtime["githubToken"])
        try:
            memory_service.retain_remediation(incident)
        except Exception:
            pass
    except Exception as error:
        pull_request_error = str(error)
        updated = repo.update_incident(incident["id"], {"remediation_error": pull_request_error})
        incident = updated or incident
        logger.warning(
            "VM incident fix failed",
            extra={"incident_id": incident["id"], "error": pull_request_error},
        )
    return {
        "incident": incident,
        "event_id": stored_event["id"],
        "pull_request_error": pull_request_error,
    }


async def sweep_heartbeats() -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    for vm in await asyncio.to_thread(repo.list_all_enabled_vms):
        last = vm.get("last_heartbeat_at")
        if not last:
            continue
        age = (now - datetime.fromisoformat(last)).total_seconds()
        if age <= settings.vm_heartbeat_timeout_seconds or vm.get("status") == "offline":
            continue
        repo.update_vm(vm["id"], {"status": "offline"})
        event = {
            "type": "heartbeat_lost",
            "signature": f"heartbeat-lost-{vm['id']}",
            "message": (
                f"No heartbeat from {vm['name']} for {int(age)}s; "
                "the VM or its watcher may be down"
            ),
            "logExcerpt": "",
        }
        if not is_duplicate(vm, event["signature"]):
            try:
                await process_event(vm, event)
            except Exception:
                logger.exception("heartbeat_lost processing failed", extra={"vm": vm["name"]})


async def vm_sweeper_loop() -> None:
    logger.info("VM heartbeat sweeper started")
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            await sweep_heartbeats()
        except Exception:
            logger.exception("VM sweep failed")
