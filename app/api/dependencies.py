"""FastAPI dependencies for application services."""

from functools import lru_cache

from app.application import WorkflowService
from app.persistence import (
    WorkflowRepository,
    configured_database_url,
    create_database_engine,
    create_session_factory,
)


@lru_cache
def _workflow_repository() -> WorkflowRepository:
    engine = create_database_engine(configured_database_url())
    return WorkflowRepository(create_session_factory(engine))


def get_workflow_service() -> WorkflowService:
    return WorkflowService(_workflow_repository())
