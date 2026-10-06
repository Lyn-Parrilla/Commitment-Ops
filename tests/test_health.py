from fastapi.testclient import TestClient


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "CommitmentOps AI"
    assert payload["version"] == "0.1.0"
    assert payload["environment"] == "test"
