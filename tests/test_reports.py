from app.reports import incident_pdf


def test_incident_report_is_a_pdf():
    report = incident_pdf(
        {
            "title": "Checkout pool exhausted",
            "service": "checkout-service",
            "severity": "SEV-2",
            "status": "resolved",
            "novelty": "recurring",
            "error_category": "database",
            "created_at": "2026-06-13T12:00:00+00:00",
            "description": "Requests returned 500 errors.",
            "diagnosis": "Connections leaked on retry.",
            "root_cause": "A session was not closed.",
            "impact": "Checkout requests failed.",
            "error_logs": "QueuePool limit reached",
            "resolution_steps": ["Close sessions", "Add pool alerts"],
            "action_items": ["Add a regression test"],
            "timeline": [{"at": "2026-06-13T12:00:00+00:00", "event": "Detected"}],
            "retrieved_memories": [],
        }
    )
    assert report.startswith(b"%PDF")
    assert len(report) > 1_000

