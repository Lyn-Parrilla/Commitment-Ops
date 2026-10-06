from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app


def test_create_app_exposes_docs_outside_production(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    get_settings.cache_clear()

    application = create_app()
    with TestClient(application) as client:
        assert client.get("/docs").status_code == 200

    get_settings.cache_clear()


def test_create_app_hides_docs_in_production(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    get_settings.cache_clear()

    application = create_app()
    with TestClient(application) as client:
        assert client.get("/docs").status_code == 404

    get_settings.cache_clear()
