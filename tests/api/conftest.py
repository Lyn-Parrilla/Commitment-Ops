from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_workflow_service
from app.application import WorkflowService
from app.config import get_settings
from app.main import create_app
from app.persistence import (
    Base,
    WorkflowRepository,
    create_database_engine,
    create_session_factory,
)


@pytest.fixture
def api_repository(tmp_path: Path) -> Iterator[WorkflowRepository]:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'api-test.db'}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    yield WorkflowRepository(create_session_factory(engine))
    engine.dispose()


@pytest.fixture
def api_client(
    api_repository: WorkflowRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setenv("ENVIRONMENT", "test")
    get_settings.cache_clear()
    application = create_app()
    service = WorkflowService(api_repository)
    application.dependency_overrides[get_workflow_service] = lambda: service

    with TestClient(application) as client:
        yield client

    application.dependency_overrides.clear()
    get_settings.cache_clear()
