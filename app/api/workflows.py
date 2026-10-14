"""Versioned accountability workflow API."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import get_workflow_service
from app.api.schemas import (
    AddRequirementRequest,
    CreateWorkflowRequest,
    ErrorResponse,
    RequirementResponse,
    RequirementReviewRequest,
    RequirementTransitionRequest,
    WorkflowResponse,
    WorkflowTransitionRequest,
)
from app.application import WorkflowService
from app.domain import WorkflowStatus, WorkflowType

router = APIRouter(prefix="/api/v1", tags=["accountability"])
Service = Annotated[WorkflowService, Depends(get_workflow_service)]

ERROR_RESPONSES = {
    404: {"model": ErrorResponse, "description": "Resource not found"},
    409: {"model": ErrorResponse, "description": "Domain state conflict"},
    422: {"model": ErrorResponse, "description": "Invalid request or domain data"},
    503: {"model": ErrorResponse, "description": "Persistence unavailable"},
}


@router.post(
    "/workflows",
    response_model=WorkflowResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
)
def create_workflow(
    payload: CreateWorkflowRequest,
    service: Service,
) -> WorkflowResponse:
    workflow = service.create_workflow(
        name=payload.name,
        workflow_type=payload.workflow_type,
    )
    return WorkflowResponse.from_domain(workflow)


@router.get(
    "/workflows",
    response_model=list[WorkflowResponse],
    responses=ERROR_RESPONSES,
)
def list_workflows(
    service: Service,
    workflow_status: Annotated[WorkflowStatus | None, Query(alias="status")] = None,
    workflow_type: WorkflowType | None = None,
) -> list[WorkflowResponse]:
    return [
        WorkflowResponse.from_domain(workflow)
        for workflow in service.list_workflows(
            status=workflow_status,
            workflow_type=workflow_type,
        )
    ]


@router.get(
    "/workflows/{workflow_id}",
    response_model=WorkflowResponse,
    responses=ERROR_RESPONSES,
)
def get_workflow(
    workflow_id: UUID,
    service: Service,
) -> WorkflowResponse:
    return WorkflowResponse.from_domain(service.get_workflow(workflow_id))


@router.post(
    "/workflows/{workflow_id}/requirements",
    response_model=RequirementResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
)
def add_requirement(
    workflow_id: UUID,
    payload: AddRequirementRequest,
    service: Service,
) -> RequirementResponse:
    requirement = service.add_requirement(workflow_id, payload.to_command())
    return RequirementResponse.from_domain(requirement)


@router.post(
    "/workflows/{workflow_id}/transitions",
    response_model=WorkflowResponse,
    responses=ERROR_RESPONSES,
)
def transition_workflow(
    workflow_id: UUID,
    payload: WorkflowTransitionRequest,
    service: Service,
) -> WorkflowResponse:
    workflow = service.transition_workflow(workflow_id, payload.action)
    return WorkflowResponse.from_domain(workflow)


@router.post(
    "/workflows/{workflow_id}/requirements/{requirement_id}/transitions",
    response_model=RequirementResponse,
    responses=ERROR_RESPONSES,
)
def transition_requirement(
    workflow_id: UUID,
    requirement_id: UUID,
    payload: RequirementTransitionRequest,
    service: Service,
) -> RequirementResponse:
    requirement = service.transition_requirement(
        workflow_id,
        requirement_id,
        payload.action,
    )
    return RequirementResponse.from_domain(requirement)


@router.post(
    "/workflows/{workflow_id}/requirements/{requirement_id}/review",
    response_model=RequirementResponse,
    responses=ERROR_RESPONSES,
)
def review_requirement(
    workflow_id: UUID,
    requirement_id: UUID,
    payload: RequirementReviewRequest,
    service: Service,
) -> RequirementResponse:
    requirement = service.review_requirement(
        workflow_id,
        requirement_id,
        payload.action,
        reviewer=payload.reviewer.to_domain() if payload.reviewer else None,
        note=payload.note,
        reason=payload.reason,
    )
    return RequirementResponse.from_domain(requirement)
