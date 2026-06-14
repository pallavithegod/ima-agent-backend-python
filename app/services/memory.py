import asyncio
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
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

    @staticmethod
    def _run(coroutine: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, coroutine).result()

    @staticmethod
    async def _complete(client: Any, coroutine: Any) -> Any:
        try:
            return await coroutine
        finally:
            await client.aclose()

    @staticmethod
    def _score(query: dict[str, Any], incident: dict[str, Any]) -> float:
        score = 0.0
        for field, weight in (
            ("service", 0.35),
            ("root_cause_type", 0.3),
            ("error_category", 0.2),
        ):
            if query.get(field) and query.get(field) == incident.get(field):
                score += weight
        query_text = " ".join(
            str(query.get(field) or "")
            for field in ("title", "description", "error_logs")
        ).lower()
        incident_text = " ".join(
            str(incident.get(field) or "")
            for field in ("title", "description", "error_logs", "root_cause")
        ).lower()
        query_tokens = set(re.findall(r"[a-z0-9_-]+", query_text))
        incident_tokens = set(re.findall(r"[a-z0-9_-]+", incident_text))
        if query_tokens and incident_tokens:
            score += 0.15 * (
                len(query_tokens & incident_tokens) / len(query_tokens | incident_tokens)
            )
        return round(min(score, 1.0), 4)

    @staticmethod
    def _tags(item: dict[str, Any], record_type: str, status: str) -> list[str]:
        raw = item.get("raw_payload") or {}
        repository = item.get("repository") or raw.get("repository") or "unknown"
        source = item.get("platform") or item.get("source") or "unknown"
        return [
            f"user:{item.get('user_id') or 'unknown'}",
            f"service:{item.get('service') or 'unknown'}",
            f"repository:{repository}",
            f"source:{source}",
            f"type:{record_type}",
            f"status:{status}",
        ]

    @staticmethod
    def _record(record_type: str, item: dict[str, Any]) -> dict[str, Any]:
        raw = item.get("raw_payload") or {}
        return {
            "schema_version": 1,
            "record_type": record_type,
            "user_id": item.get("user_id"),
            "incident_id": item.get("id") if record_type != "deployment_failure" else None,
            "deployment_id": item.get("deployment_id")
            or (item.get("id") if record_type == "deployment_failure" else None),
            "source": item.get("platform") or item.get("source"),
            "service": item.get("service"),
            "repository": item.get("repository") or raw.get("repository"),
            "commit_sha": item.get("commit_sha"),
            "file_path": item.get("file_path") or raw.get("file_path"),
            "status": item.get("status"),
            "title": item.get("title"),
            "message": item.get("message"),
            "error_category": item.get("error_category"),
            "root_cause_type": item.get("root_cause_type"),
            "error_logs": item.get("error_logs") or raw.get("logs"),
            "diagnosis": item.get("diagnosis"),
            "root_cause": item.get("root_cause"),
            "resolution_steps": item.get("resolution_steps") or [],
            "fix_summary": item.get("fix_summary"),
            "fix_rationale": item.get("fix_rationale"),
            "fix_diff": item.get("fix_diff"),
            "pull_request_url": item.get("pull_request_url"),
            "remediation_error": item.get("remediation_error"),
            "created_at": item.get("created_at"),
            "resolved_at": item.get("resolved_at"),
        }

    @classmethod
    def _stored_record(cls, value: Any) -> dict[str, Any] | None:
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
        if isinstance(value, dict):
            for key in ("content", "text", "fact"):
                record = cls._stored_record(value.get(key))
                if record:
                    return record
            for key in ("chunks", "source_facts"):
                record = cls._stored_record(value.get(key))
                if record:
                    return record
        if isinstance(value, list):
            for item in value:
                record = cls._stored_record(item)
                if record:
                    return record
        return None

    @classmethod
    def _records_from_chunks(cls, chunks: dict[str, Any]) -> list[dict[str, Any]]:
        groups: dict[str, list[tuple[int, str]]] = {}
        for key, value in chunks.items():
            raw = value.model_dump() if hasattr(value, "model_dump") else value
            if not isinstance(raw, dict) or not raw.get("text"):
                continue
            chunk_id = str(raw.get("id") or key)
            group_id, separator, suffix = chunk_id.rpartition("_")
            if not separator or not suffix.isdigit():
                group_id = chunk_id
            chunk_index = int(raw.get("chunk_index") or (suffix if suffix.isdigit() else 0))
            groups.setdefault(group_id, []).append((chunk_index, str(raw["text"])))

        records: list[dict[str, Any]] = []
        for parts in groups.values():
            content = "\n".join(text for _, text in sorted(parts))
            record = cls._stored_record(content)
            if record:
                records.append(record)
        return records

    def recall(self, query: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
        client = self._client()
        if client is None:
            return []
        recall_query = {
            key: query.get(key)
            for key in (
                "title",
                "description",
                "service",
                "service_hint",
                "error_category",
                "root_cause_type",
                "repository",
                "file_path",
            )
            if query.get(key)
        }
        if query.get("error_logs"):
            recall_query["error_logs"] = str(query["error_logs"])[-1_200:]
        user_id = str(query.get("user_id") or "unknown")
        response = self._run(self._complete(
            client,
            client.arecall(
                bank_id=self.settings.hindsight_bank_id,
                query=json.dumps(recall_query, default=str),
                tags=[f"user:{user_id}"],
                tags_match="all_strict",
                budget="high",
                max_tokens=5000,
                include_chunks=True,
                max_chunk_tokens=5000,
                include_source_facts=True,
            ),
        ))
        payload = response.model_dump() if hasattr(response, "model_dump") else response
        items = payload.get("results") or payload.get("memories") or payload.get("facts") or []
        chunks = payload.get("chunks") or {}
        source_facts = payload.get("source_facts") or {}
        chunk_by_id = (
            chunks
            if isinstance(chunks, dict)
            else {
                chunk.get("id"): chunk
                for chunk in chunks
                if isinstance(chunk, dict) and chunk.get("id")
            }
        )
        source_fact_by_id = (
            source_facts
            if isinstance(source_facts, dict)
            else {
                fact.get("id"): fact
                for fact in source_facts
                if isinstance(fact, dict) and fact.get("id")
            }
        )
        matches: list[dict[str, Any]] = []
        seen: set[str] = set()
        stored_records = self._records_from_chunks(chunk_by_id)
        stored_records.sort(
            key=lambda record: self._score(query, record),
            reverse=True,
        )
        for incident in stored_records:
            identity = json.dumps(incident, sort_keys=True, default=str)
            if identity in seen:
                continue
            seen.add(identity)
            matches.append({
                "similarity": self._score(query, incident),
                "incident": self._memory_view(incident),
            })
            if len(matches) >= limit:
                return matches

        for item in items:
            raw = item.model_dump() if hasattr(item, "model_dump") else item
            incident = self._stored_record(raw)
            for source_fact_id in raw.get("source_fact_ids") or []:
                fact = source_fact_by_id.get(source_fact_id) or {}
                incident = incident or self._stored_record(fact)
                incident = incident or self._stored_record(
                    chunk_by_id.get(fact.get("chunk_id"))
                )
            if not incident:
                text = raw.get("text") or raw.get("fact")
                if not text:
                    continue
                incident = {
                    "record_type": "memory_fact",
                    "title": text,
                    "root_cause": text,
                    "repository": raw.get("metadata", {}).get("repository"),
                    "commit_sha": raw.get("metadata", {}).get("commit_sha"),
                    "created_at": raw.get("mentioned_at"),
                }
            identity = json.dumps(incident, sort_keys=True, default=str)
            if identity in seen:
                continue
            seen.add(identity)
            matches.append({
                "similarity": float(raw.get("relevance") or raw.get("score") or 0.75),
                "incident": self._memory_view(incident),
            })
            if len(matches) >= limit:
                break
        return matches

    def retain(self, incident: dict[str, Any]) -> None:
        client = self._client()
        if client is None:
            return
        record = self._record("incident", incident)
        self._run(self._complete(
            client,
            client.aretain(
                bank_id=self.settings.hindsight_bank_id,
                content=json.dumps(record, indent=2, default=str),
                context="Production incident record with diagnosis and current resolution state",
                document_id=incident["id"],
                metadata={
                    "incident_id": incident["id"],
                    "service": incident["service"],
                    "severity": incident["severity"],
                    "error_category": incident["error_category"],
                    "root_cause_type": incident["root_cause_type"],
                },
                tags=self._tags(incident, "incident", incident.get("status") or "open"),
            ),
        ))

    def retain_remediation(self, incident: dict[str, Any]) -> None:
        client = self._client()
        if client is None:
            return
        content = self._record("remediation", incident)
        self._run(self._complete(client, client.aretain(
            bank_id=self.settings.hindsight_bank_id,
            content=json.dumps(content, indent=2, default=str),
            context="Production deployment failure, agent diagnosis, proposed code fix, and draft pull request",
            document_id=f"remediation-{incident['id']}",
            metadata={
                "incident_id": incident["id"],
                "deployment_id": incident.get("deployment_id") or "",
                "repository": incident.get("repository") or "",
                "commit_sha": incident.get("commit_sha") or "",
                "pull_request_url": incident.get("pull_request_url") or "",
            },
            tags=self._tags(
                incident,
                "remediation",
                "pr-created" if incident.get("pull_request_url") else "fix-proposed",
            ),
        )))

    def retain_failure(self, deployment: dict[str, Any]) -> None:
        client = self._client()
        if client is None:
            return
        raw = deployment.get("raw_payload") or {}
        self._run(self._complete(client, client.aretain(
            bank_id=self.settings.hindsight_bank_id,
            content=json.dumps(
                self._record("deployment_failure", deployment),
                indent=2,
                default=str,
            ),
            context="Raw production deployment failure and captured build evidence",
            document_id=f"failure-{deployment['id']}",
            metadata={
                "deployment_id": deployment["id"],
                "repository": raw.get("repository") or "",
                "commit_sha": deployment.get("commit_sha") or "",
            },
            tags=self._tags(deployment, "deployment-failure", "failed"),
        )))

    @staticmethod
    def _memory_view(incident: dict[str, Any]) -> dict[str, Any]:
        record_type = incident.get("record_type")
        return {
            "id": incident.get("incident_id") or incident.get("id")
            or incident.get("deployment_id"),
            "record_type": record_type or "incident",
            "title": incident.get("title") or incident.get("message")
            or "Deployment failure",
            "service": incident.get("service"),
            "severity": incident.get("severity"),
            "error_category": incident.get("error_category"),
            "root_cause_type": incident.get("root_cause_type"),
            "root_cause": incident.get("root_cause") or incident.get("diagnosis"),
            "resolution_steps": incident.get("resolution_steps") or [],
            "repository": incident.get("repository"),
            "commit_sha": incident.get("commit_sha"),
            "file_path": incident.get("file_path"),
            "error_logs": str(incident.get("error_logs") or "")[-2_000:],
            "fix_summary": incident.get("fix_summary"),
            "fix_rationale": incident.get("fix_rationale"),
            "pull_request_url": incident.get("pull_request_url"),
            "created_at": incident.get("created_at"),
            "time_to_resolve_minutes": incident.get("time_to_resolve_minutes"),
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
