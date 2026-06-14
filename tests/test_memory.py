import json

from app.services.memory import MemoryService


def test_matching_service_and_root_type_scores_highest():
    service = MemoryService()
    query = {
        "title": "checkout pool timeout",
        "description": "connections exhausted",
        "service": "checkout-service",
        "error_category": "database",
        "root_cause_type": "connection-pool-exhaustion",
    }
    matching = {
        **query,
        "error_logs": "QueuePool limit reached",
        "root_cause": "connections leaked",
        "resolution_steps": ["close sessions"],
    }
    unrelated = {
        **matching,
        "service": "auth-service",
        "error_category": "authentication",
        "root_cause_type": "token-misconfiguration",
    }
    assert service._score(query, matching) > service._score(query, unrelated)


def test_cloud_records_are_readable_and_user_scoped():
    deployment = {
        "id": "dep-123",
        "user_id": "user-7",
        "platform": "render",
        "service": "checkout-api",
        "status": "build_failed",
        "commit_sha": "abc123",
        "message": "Dependency install failed",
        "raw_payload": {
            "repository": "example/checkout",
            "logs": "npm error ERESOLVE",
        },
        "created_at": "2026-06-14T10:00:00+00:00",
    }
    record = MemoryService._record("deployment_failure", deployment)
    tags = MemoryService._tags(deployment, "deployment-failure", "failed")

    assert record["deployment_id"] == "dep-123"
    assert record["source"] == "render"
    assert record["error_logs"] == "npm error ERESOLVE"
    assert "user:user-7" in tags
    assert "repository:example/checkout" in tags


def test_recall_parser_reads_original_json_from_source_chunks():
    record = {"record_type": "remediation", "fix_summary": "Pin React 18"}
    recalled = {
        "text": "The dependency issue was fixed.",
        "chunks": [{"text": json.dumps(record)}],
    }

    assert MemoryService._stored_record(recalled) == record


def test_recall_response_links_source_fact_to_original_chunk():
    record = {"record_type": "remediation", "fix_summary": "Pin React 18"}
    payload = {
        "results": [{"text": "A dependency conflict was fixed.", "source_fact_ids": ["fact-1"]}],
        "source_facts": {"fact-1": {"id": "fact-1", "chunk_id": "chunk-1"}},
        "chunks": {"chunk-1": {"id": "chunk-1", "text": json.dumps(record)}},
    }

    fact = payload["source_facts"][payload["results"][0]["source_fact_ids"][0]]
    recalled = MemoryService._stored_record(payload["chunks"][fact["chunk_id"]])

    assert recalled == record


def test_recall_reconstructs_json_split_across_document_chunks():
    chunks = {
        "bank_remediation-1_1": {
            "id": "bank_remediation-1_1",
            "chunk_index": 1,
            "text": '  "fix_summary": "Pin React 18"\n}',
        },
        "bank_remediation-1_0": {
            "id": "bank_remediation-1_0",
            "chunk_index": 0,
            "text": '{\n  "record_type": "remediation",',
        },
    }

    assert MemoryService._records_from_chunks(chunks) == [{
        "record_type": "remediation",
        "fix_summary": "Pin React 18",
    }]


def test_memory_view_keeps_distilled_hindsight_fact():
    fact = {
        "record_type": "memory_fact",
        "title": "Pin React 18 to resolve the peer dependency conflict.",
        "root_cause": "Pin React 18 to resolve the peer dependency conflict.",
    }

    view = MemoryService._memory_view(fact)

    assert view["record_type"] == "memory_fact"
    assert "React 18" in view["root_cause"]

