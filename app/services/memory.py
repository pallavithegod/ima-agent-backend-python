import json
from collections import Counter
from typing import Any

from ..config import get_settings
from ..database import list_incidents


class MemoryService:
    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def mode(self) -> str:
        return "hindsight-cloud" if self.settings.hindsight_api_url else "unconfigured"

    def _client(self):
        if not self.settings.hindsight_api_url:
            return None
        from hindsight_client import Hindsight

        return Hindsight(
            base_url=self.settings.hindsight_api_url,
            api_key=self.settings.hindsight_api_key or None,
            timeout=30,
        )

    def recall(self, query: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
        client = self._client()
        if client is None:
            return []
        service = str(query.get("service") or query.get("service_hint") or "unknown")
        response = client.recall(
            bank_id=self.settings.hindsight_bank_id,
            query=json.dumps(query, default=str),
            tags=[f"service:{service}"],
            tags_match="all_strict",
            budget="high",
            max_tokens=5000,
        )
        payload = response.model_dump() if hasattr(response, "model_dump") else response
        items = payload.get("results") or payload.get("memories") or payload.get("facts") or []
        matches: list[dict[str, Any]] = []
        for item in items[:limit]:
            raw = item.model_dump() if hasattr(item, "model_dump") else item
            content = raw.get("content") or raw.get("text") or raw.get("fact") or ""
            try:
                incident = json.loads(content)
            except (TypeError, json.JSONDecodeError):
                continue
            matches.append({
                "similarity": float(raw.get("relevance") or raw.get("score") or 0.75),
                "incident": self._memory_view(incident),
            })
        return matches

    def retain(self, incident: dict[str, Any]) -> None:
        client = self._client()
        if client is None:
            return
        client.retain(
            bank_id=self.settings.hindsight_bank_id,
            content=json.dumps(incident, default=str),
            context="Resolved production deployment incident and its verified remediation",
            document_id=incident["id"],
            metadata={
                "incident_id": incident["id"],
                "service": incident["service"],
                "severity": incident["severity"],
                "error_category": incident["error_category"],
                "root_cause_type": incident["root_cause_type"],
            },
            tags=[f"service:{incident['service']}", "source:vercel", "status:resolved"],
        )

    def retain_remediation(self, incident: dict[str, Any]) -> None:
        client = self._client()
        if client is None:
            return
        content = {
            "incident_id": incident["id"],
            "deployment_id": incident.get("deployment_id"),
            "repository": incident.get("repository"),
            "commit_sha": incident.get("commit_sha"),
            "file_path": incident.get("file_path"),
            "error_logs": incident.get("error_logs"),
            "diagnosis": incident.get("diagnosis"),
            "root_cause": incident.get("root_cause"),
            "fix_summary": incident.get("fix_summary"),
            "fix_rationale": incident.get("fix_rationale"),
            "fix_diff": incident.get("fix_diff"),
            "pull_request_url": incident.get("pull_request_url"),
            "created_at": incident.get("created_at"),
        }
        client.retain(
            bank_id=self.settings.hindsight_bank_id,
            content=json.dumps(content, default=str),
            context="Production deployment failure, agent diagnosis, proposed code fix, and draft pull request",
            document_id=f"remediation-{incident['id']}",
            metadata={
                "incident_id": incident["id"],
                "deployment_id": incident.get("deployment_id") or "",
                "repository": incident.get("repository") or "",
                "commit_sha": incident.get("commit_sha") or "",
                "pull_request_url": incident.get("pull_request_url") or "",
            },
            tags=[
                f"service:{incident['service']}",
                f"repository:{incident.get('repository') or 'unknown'}",
                "source:vercel",
                "type:remediation",
                "status:pr-created" if incident.get("pull_request_url") else "status:fix-proposed",
            ],
        )

    def retain_failure(self, deployment: dict[str, Any]) -> None:
        client = self._client()
        if client is None:
            return
        raw = deployment.get("raw_payload") or {}
        client.retain(
            bank_id=self.settings.hindsight_bank_id,
            content=json.dumps(
                {
                    "deployment_id": deployment["id"],
                    "repository": raw.get("repository"),
                    "commit_sha": deployment.get("commit_sha"),
                    "status": deployment.get("status"),
                    "message": deployment.get("message"),
                    "logs": raw.get("logs"),
                    "created_at": deployment.get("created_at"),
                },
                default=str,
            ),
            context="Raw production deployment failure and captured build evidence",
            document_id=f"failure-{deployment['id']}",
            metadata={
                "deployment_id": deployment["id"],
                "repository": raw.get("repository") or "",
                "commit_sha": deployment.get("commit_sha") or "",
            },
            tags=[
                f"service:{deployment['service']}",
                f"repository:{raw.get('repository') or 'unknown'}",
                "source:vercel",
                "type:deployment-failure",
            ],
        )

    @staticmethod
    def _memory_view(incident: dict[str, Any]) -> dict[str, Any]:
        return {
            key: incident.get(key)
            for key in (
                "id", "title", "service", "severity", "error_category",
                "root_cause_type", "root_cause", "resolution_steps",
                "created_at", "time_to_resolve_minutes",
            )
        }

    def trends(self) -> list[dict[str, Any]]:
        resolved = [item for item in list_incidents(limit=500) if item["status"] == "resolved"]
        counts = Counter((item["service"], item["root_cause_type"]) for item in resolved)
        return [
            {
                "service": service,
                "pattern": root_type,
                "count": count,
                "recommendation": f"{service} has repeated {root_type} incidents. Add a systemic guardrail.",
            }
            for (service, root_type), count in counts.most_common()
            if count >= 2
        ]


memory_service = MemoryService()
