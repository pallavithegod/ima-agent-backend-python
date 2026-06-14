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

