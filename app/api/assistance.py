"""Versioned assistance proposal API."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.api.dependencies import get_assistance_service
from app.api.schemas import (
    AssistanceDecisionRequest,
    AssistanceProposalResponse,
    ErrorResponse,
)
from app.application.assistance import AssistanceService

router = APIRouter(prefix="/api/v1", tags=["assistance"])
Service = Annotated[AssistanceService, Depends(get_assistance_service)]

ERROR_RESPONSES = {
    404: {"model": ErrorResponse, "description": "Resource not found"},
    409: {"model": ErrorResponse, "description": "Domain state conflict"},
    422: {"model": ErrorResponse, "description": "Invalid request or domain data"},
    503: {"model": ErrorResponse, "description": "Persistence unavailable"},
}


@router.post(
    "/workflows/{workflow_id}/assistance-proposals",
    response_model=list[AssistanceProposalResponse],
    responses=ERROR_RESPONSES,
)
def detect_assistance_proposals(
    workflow_id: UUID,
    service: Service,
) -> list[AssistanceProposalResponse]:
    return [
        AssistanceProposalResponse.from_stored(proposal)
        for proposal in service.detect(workflow_id)
    ]


@router.get(
    "/workflows/{workflow_id}/assistance-proposals",
    response_model=list[AssistanceProposalResponse],
    responses=ERROR_RESPONSES,
)
def list_assistance_proposals(
    workflow_id: UUID,
    service: Service,
) -> list[AssistanceProposalResponse]:
    return [
        AssistanceProposalResponse.from_stored(proposal)
        for proposal in service.list_proposals(workflow_id)
    ]


@router.get(
    "/workflows/{workflow_id}/assistance-proposals/{proposal_id}",
    response_model=AssistanceProposalResponse,
    responses=ERROR_RESPONSES,
)
def get_assistance_proposal(
    workflow_id: UUID,
    proposal_id: UUID,
    service: Service,
) -> AssistanceProposalResponse:
    return AssistanceProposalResponse.from_stored(
        service.get_proposal(workflow_id, proposal_id)
    )


@router.post(
    "/workflows/{workflow_id}/assistance-proposals/{proposal_id}/decision",
    response_model=AssistanceProposalResponse,
    responses=ERROR_RESPONSES,
)
def decide_assistance_proposal(
    workflow_id: UUID,
    proposal_id: UUID,
    payload: AssistanceDecisionRequest,
    service: Service,
) -> AssistanceProposalResponse:
    return AssistanceProposalResponse.from_stored(
        service.decide(workflow_id, proposal_id, payload.action)
    )


@router.post(
    "/workflows/{workflow_id}/assistance-proposals/{proposal_id}/execute",
    response_model=AssistanceProposalResponse,
    status_code=status.HTTP_200_OK,
    responses=ERROR_RESPONSES,
)
def execute_assistance_proposal(
    workflow_id: UUID,
    proposal_id: UUID,
    service: Service,
) -> AssistanceProposalResponse:
    return AssistanceProposalResponse.from_stored(
        service.execute(workflow_id, proposal_id)
    )
