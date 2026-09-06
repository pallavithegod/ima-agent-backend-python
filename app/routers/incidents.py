from collections import Counter
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from ..db.repo import get_incident, list_deployments, list_incidents, update_incident
from ..models import IncidentResolve, RepairApproval
from ..security import current_user
from ..services.integrations import IntegrationConfigurationError
from ..services.memory import memory_service
from ..services.remediation import remediation_service

router = APIRouter(prefix="/api", tags=["incidents"])


@router.get("/incidents")
def incidents(
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    user: dict[str, Any] = Depends(current_user),
) -> list[dict[str, Any]]:
    return list_incidents(limit=limit, status=status, user_id=str(user["sub"]))


@router.get("/incidents/{incident_id}")
def incident_detail(
    incident_id: str, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    incident = get_incident(incident_id, str(user["sub"]))
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


@router.patch("/incidents/{incident_id}/resolve")
def resolve_incident(
    incident_id: str,
    payload: IncidentResolve,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    incident = get_incident(incident_id, str(user["sub"]))
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    now = datetime.now(UTC).isoformat()
    timeline = [
        *incident["timeline"],
        {"at": now, "event": f"Resolved by {payload.engineer}"},
    ]
    updated = update_incident(
        incident_id,
        {
            **payload.model_dump(exclude_none=True),
            "status": "resolved",
            "resolved_at": now,
            "timeline": timeline,
        },
    )
    if updated:
        memory_service.retain(updated)
    return updated or incident


@router.get("/analytics")
def analytics(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    items = list_incidents(limit=500, user_id=str(user["sub"]))
    resolved = [item for item in items if item["status"] == "resolved"]
    durations = [
        item["time_to_resolve_minutes"]
        for item in resolved
        if item["time_to_resolve_minutes"] is not None
    ]
    service_counts = Counter(item["service"] for item in items)
    category_counts = Counter(item["error_category"] for item in items)
    severity_counts = Counter(item["severity"] for item in items)
    novelty_counts = Counter(item["novelty"] for item in items)
    return {
        "totals": {
            "incidents": len(items),
            "open": sum(item["status"] == "open" for item in items),
            "resolved": len(resolved),
            "recurring": sum(item["novelty"] == "recurring" for item in items),
            "average_mttr": round(sum(durations) / len(durations)) if durations else 0,
        },
        "by_service": [{"name": key, "value": value} for key, value in service_counts.items()],
        "by_category": [{"name": key, "value": value} for key, value in category_counts.items()],
        "by_severity": [{"name": key, "value": value} for key, value in severity_counts.items()],
        "by_novelty": [{"name": key, "value": value} for key, value in novelty_counts.items()],
        "trends": memory_service.trends(),
        "recent": items[:8],
    }


@router.get("/handoff")
def handoff(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    items = list_incidents(limit=30, user_id=str(user["sub"]))
    open_items = [item for item in items if item["status"] == "open"]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "open_incidents": open_items,
        "recently_resolved": [item for item in items if item["status"] == "resolved"][:5],
        "attention": [
            {
                "incident_id": item["id"],
                "next_step": item["resolution_steps"][0]
                if item["resolution_steps"]
                else "Continue diagnosis",
            }
            for item in open_items
        ],
        "trends": memory_service.trends()[:5],
    }


@router.get("/incidents/{incident_id}/report.pdf")
def download_report(
    incident_id: str, user: dict[str, Any] = Depends(current_user)
) -> Response:
    incident = get_incident(incident_id, str(user["sub"]))
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    try:
        from ..reports import incident_pdf
    except ModuleNotFoundError as error:
        if error.name != "reportlab":
            raise
        raise HTTPException(
            status_code=503,
            detail="PDF reports are unavailable. Install backend dependencies from requirements.txt.",
        ) from error
    return Response(
        incident_pdf(incident),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="incident-{incident_id[:8]}.pdf"'},
    )


@router.post("/incidents/{incident_id}/create-draft-pr")
async def create_draft_pr(
    incident_id: str,
    payload: RepairApproval,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    if not payload.approved:
        raise HTTPException(status_code=400, detail="Explicit approval is required")
    try:
        return await remediation_service.create_draft_pr(incident_id, str(user["sub"]))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except IntegrationConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.get("/deployments")
def deployments(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    return list_deployments(user_id=str(user["sub"]))
