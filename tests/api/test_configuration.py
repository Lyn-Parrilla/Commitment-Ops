from fastapi.testclient import TestClient

from app.api.dependencies import _workflow_repository
from app.config import get_settings
from app.main import create_app


def test_missing_database_url_returns_bounded_503_and_preserves_health(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", "")
    get_settings.cache_clear()
    _workflow_repository.cache_clear()
    application = create_app()

    try:
        with TestClient(application) as client:
            workflow_response = client.post(
                "/api/v1/workflows",
                json={
                    "name": "Synthetic workflow",
                    "workflow_type": "grant",
                },
            )
            health_response = client.get("/health")
    finally:
        _workflow_repository.cache_clear()
        get_settings.cache_clear()

    assert workflow_response.status_code == 503
    assert workflow_response.json() == {
        "error": {
            "code": "persistence_unavailable",
            "message": (
                "The accountability store could not complete the operation"
            ),
        }
    }
    response_text = workflow_response.text.lower()
    assert "database_url" not in response_text
    assert "credential" not in response_text
    assert "traceback" not in response_text
    assert "postgresql://" not in response_text
    assert health_response.status_code == 200
    assert health_response.json()["status"] == "ok"
