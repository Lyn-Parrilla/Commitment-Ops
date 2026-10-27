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
from app.application.assistance import AssistanceService
from app.application.discovery import DiscoveryService
from app.application.submissions import SubmissionService
from app.domain import Person
from app.persistence import (
    AssistanceRepository,
    DiscoveryRepository,
    OrganizationRepository,
    SessionFactory,
    WorkflowRepository,
    configured_database_url,
    create_database_engine,
    create_session_factory,
)
from app.persistence.submissions import SubmissionRepository


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


@lru_cache
def _discovery_repository() -> DiscoveryRepository:
    return DiscoveryRepository(_session_factory())


def get_discovery_repository() -> DiscoveryRepository:
    return _discovery_repository()


@lru_cache
def _assistance_repository() -> AssistanceRepository:
    return AssistanceRepository(_session_factory())


def get_assistance_repository() -> AssistanceRepository:
    return _assistance_repository()


def get_organization_repository() -> OrganizationRepository:
    return _organization_repository()


@lru_cache
def _submission_repository() -> SubmissionRepository:
    from app.config import get_settings
    from app.persistence.object_storage import configured_object_storage

    return SubmissionRepository(
        _session_factory(),
        configured_object_storage(get_settings()),
    )


def get_submission_repository() -> SubmissionRepository:
    return _submission_repository()


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


def get_discovery_service(
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(get_authenticated_principal),
    ],
    organization_id: Annotated[UUID, Header(alias="X-Organization-ID")],
    discovery_repository: Annotated[
        DiscoveryRepository,
        Depends(get_discovery_repository),
    ],
    organization_repository: Annotated[
        OrganizationRepository,
        Depends(get_organization_repository),
    ],
) -> DiscoveryService:
    if not organization_repository.has_active_membership(
        organization_id=organization_id,
        user_id=principal.user_id,
    ):
        raise OrganizationAccessDeniedError

    reviewer = Person.create(
        name=principal.email,
        email=principal.email,
    ).model_copy(update={"id": principal.user_id})
    return DiscoveryService(
        discovery_repository,
        organization_id=organization_id,
        user_id=principal.user_id,
        reviewer=reviewer,
    )


def get_assistance_service(
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(get_authenticated_principal),
    ],
    organization_id: Annotated[UUID, Header(alias="X-Organization-ID")],
    workflow_repository: Annotated[
        WorkflowRepository,
        Depends(get_workflow_repository),
    ],
    assistance_repository: Annotated[
        AssistanceRepository,
        Depends(get_assistance_repository),
    ],
    organization_repository: Annotated[
        OrganizationRepository,
        Depends(get_organization_repository),
    ],
) -> AssistanceService:
    if not organization_repository.has_active_membership(
        organization_id=organization_id,
        user_id=principal.user_id,
    ):
        raise OrganizationAccessDeniedError
    return AssistanceService(
        workflow_repository,
        assistance_repository,
        organization_id=organization_id,
        user_id=principal.user_id,
    )


def get_submission_service(
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(get_authenticated_principal),
    ],
    organization_id: Annotated[UUID, Header(alias="X-Organization-ID")],
    workflow_repository: Annotated[
        WorkflowRepository,
        Depends(get_workflow_repository),
    ],
    submission_repository: Annotated[
        SubmissionRepository,
        Depends(get_submission_repository),
    ],
    organization_repository: Annotated[
        OrganizationRepository,
        Depends(get_organization_repository),
    ],
) -> SubmissionService:
    if not organization_repository.has_active_membership(
        organization_id=organization_id,
        user_id=principal.user_id,
    ):
        raise OrganizationAccessDeniedError
    return SubmissionService(
        workflow_repository,
        submission_repository,
        organization_id=organization_id,
        user_id=principal.user_id,
    )
