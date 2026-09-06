from fastapi.testclient import TestClient

from app.main import app


def _register_user(client: TestClient, email: str) -> str:
    response = client.post(
        "/api/auth/register",
        json={"name": "VM Tester", "email": email, "password": "password123"},
    )
    assert response.status_code == 201
    return response.json()["token"]


def test_vm_lifecycle_and_event_dedup(monkeypatch):
    with TestClient(app) as client:
        token = _register_user(client, "vm-tests@example.com")
        headers = {"Authorization": f"Bearer {token}"}

        created = client.post(
            "/api/vms",
            json={
                "name": "app-one",
                "serviceName": "flaky-shop",
                "repository": "acme/flaky-shop",
            },
            headers=headers,
        )
        assert created.status_code == 201
        vm = created.json()["vm"]
        api_key = created.json()["apiKey"]
        assert "api_key_hash" not in vm

        # duplicate name rejected
        duplicate = client.post(
            "/api/vms",
            json={
                "name": "app-one",
                "serviceName": "another-service",
                "repository": "acme/another",
            },
            headers=headers,
        )
        assert duplicate.status_code == 409

        # bad key rejected
        assert (
            client.post(
                f"/api/vms/{vm['id']}/heartbeat",
                json={"status": "online"},
                headers={"X-VM-Api-Key": "wrong"},
            ).status_code
            == 401
        )

        beat = client.post(
            f"/api/vms/{vm['id']}/heartbeat",
            json={"status": "online"},
            headers={"X-VM-Api-Key": api_key},
        )
        assert beat.status_code == 200
        listed = client.get("/api/vms", headers=headers).json()["vms"]
        assert listed[0]["status"] == "online"

        # crash event creates an incident through the LangGraph pipeline
        # (local LLM fallback); the fixer fails without a GitHub connection,
        # which the endpoint reports as pullRequestError.
        event = {
            "type": "crash",
            "signature": "sig-abc",
            "message": "process exited with code 1",
            "exitCode": 1,
            "logExcerpt": "TypeError: Cannot read properties of undefined at src/pricing.js:7",
        }
        first = client.post(
            f"/api/vms/{vm['id']}/events", json=event, headers={"X-VM-Api-Key": api_key}
        )
        assert first.status_code == 201
        body = first.json()
        assert body["deduplicated"] is False
        assert body["incidentId"]
        assert body["pullRequestError"]

        # identical signature inside the cooldown window is deduplicated
        second = client.post(
            f"/api/vms/{vm['id']}/events", json=event, headers={"X-VM-Api-Key": api_key}
        )
        assert second.json() == {"deduplicated": True}

        incidents = client.get("/api/incidents", headers=headers).json()
        assert any(item["source"] == "vm" for item in incidents)
        deployments = client.get("/api/deployments", headers=headers).json()
        assert any(item["platform"] == "vm" for item in deployments)
