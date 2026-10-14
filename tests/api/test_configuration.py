from uuid import UUID

from fastapi.testclient import TestClient

from app.api.auth import AuthenticatedPrincipal, get_authenticated_principal
from app.api.dependencies import (
    _organization_repository,
    _session_factory,
    _workflow_repository,
)
from app.config import get_settings
from app.main import create_app


def test_missing_database_url_returns_bounded_503_and_preserves_health(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", "")
    get_settings.cache_clear()
    _workflow_repository.cache_clear()
    _organization_repository.cache_clear()
    _session_factory.cache_clear()
    application = create_app()
    application.dependency_overrides[get_authenticated_principal] = lambda: (
        AuthenticatedPrincipal(
            user_id=UUID("20000000-0000-0000-0000-000000000001"),
            email="reviewer@example.test",
        )
    )

    try:
        with TestClient(application) as client:
            workflow_response = client.post(
                "/api/v1/workflows",
                json={
                    "name": "Synthetic workflow",
                    "workflow_type": "grant",
                },
                headers={
                    "X-Organization-ID": (
                        "30000000-0000-0000-0000-000000000001"
                    )
                },
            )
            health_response = client.get("/health")
    finally:
        _workflow_repository.cache_clear()
        _organization_repository.cache_clear()
        _session_factory.cache_clear()
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
