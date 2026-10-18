"""FastAPI dependencies for application services."""

from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header

from app.api.auth import (
    AuthenticatedPrincipal,
    OrganizationAccessDeniedError,
    get_authenticated_principal,
)
from app.application import WorkflowService
from app.domain import Person
from app.persistence import (
    OrganizationRepository,
    SessionFactory,
    WorkflowRepository,
    configured_database_url,
    create_database_engine,
    create_session_factory,
)


@lru_cache
def _workflow_repository() -> WorkflowRepository:
    return WorkflowRepository(_session_factory())


@lru_cache
def _organization_repository() -> OrganizationRepository:
    return OrganizationRepository(_session_factory())


@lru_cache
def _session_factory() -> SessionFactory:
    engine = create_database_engine(configured_database_url())
    return create_session_factory(engine)


def get_workflow_repository() -> WorkflowRepository:
    return _workflow_repository()


def get_organization_repository() -> OrganizationRepository:
    return _organization_repository()


def get_workflow_service(
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(get_authenticated_principal),
    ],
    organization_id: Annotated[UUID, Header(alias="X-Organization-ID")],
    workflow_repository: Annotated[
        WorkflowRepository,
        Depends(get_workflow_repository),
    ],
    organization_repository: Annotated[
        OrganizationRepository,
        Depends(get_organization_repository),
    ],
) -> WorkflowService:
    if not organization_repository.has_active_membership(
        organization_id=organization_id,
        user_id=principal.user_id,
    ):
        raise OrganizationAccessDeniedError

    reviewer = Person.create(
        name=principal.email,
        email=principal.email,
    ).model_copy(update={"id": principal.user_id})
    return WorkflowService(
        workflow_repository,
        organization_id=organization_id,
        user_id=principal.user_id,
        actor_reference=principal.actor_reference,
        reviewer=reviewer,
    )
