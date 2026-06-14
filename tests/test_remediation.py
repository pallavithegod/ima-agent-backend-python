from app.services.integrations import VercelService
from app.services.remediation import RemediationService


def test_vercel_git_metadata_reads_github_commit_fields():
    metadata = VercelService.git_metadata(
        {
            "meta": {
                "githubOrg": "acme",
                "githubRepo": "checkout",
                "githubCommitSha": "abc123",
                "githubCommitRef": "main",
                "githubCommitMessage": "break build",
            }
        }
    )
    assert metadata == {
        "repository": "acme/checkout",
        "commit_sha": "abc123",
        "git_ref": "main",
        "commit_message": "break build",
    }


def test_select_file_prefers_stack_trace_file_changed_by_commit():
    logs = "Build failed\nat src/payments/charge.ts:42:11\n"
    selected = RemediationService._select_file(
        logs,
        ["src/payments/charge.ts", "src/index.ts"],
    )
    assert selected == ("src/payments/charge.ts", 42)


def test_snippet_centers_on_reported_line():
    source = "\n".join(f"line {index}" for index in range(1, 30))
    snippet = RemediationService._snippet(source, 15, radius=2)
    assert "   15 | line 15" in snippet
    assert "   13 | line 13" in snippet


def test_existing_pull_request_is_idempotent():
    incident = {
        "id": "incident-1",
        "pull_request_url": "https://github.com/acme/api/pull/12",
    }
    service = RemediationService()

    import asyncio

    result = asyncio.run(service._create_draft_pr_from_runtime(incident, {}))
    assert result is incident
