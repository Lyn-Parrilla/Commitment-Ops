"""Versioned discovery review and feedback API."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.api.dependencies import get_discovery_service
from app.api.schemas import (
    CandidateResponse,
    CandidateReviewBody,
    CandidateRevisionRequest,
    DiscoverCandidateRequest,
    ErrorResponse,
    HumanAddedFeedbackRequest,
    LearningSignalResponse,
)
from app.application.discovery import DiscoveryService
from app.domain import CandidateRevision, Requirement, SourceDocument

router = APIRouter(prefix="/api/v1/discovery", tags=["discovery"])
Service = Annotated[DiscoveryService, Depends(get_discovery_service)]

ERROR_RESPONSES = {
    404: {"model": ErrorResponse, "description": "Resource not found"},
    409: {"model": ErrorResponse, "description": "Domain state conflict"},
    422: {"model": ErrorResponse, "description": "Invalid request or domain data"},
    503: {"model": ErrorResponse, "description": "Persistence unavailable"},
}


@router.post(
    "/candidates",
    response_model=CandidateResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
)
def discover_candidate(
    payload: DiscoverCandidateRequest,
    service: Service,
) -> CandidateResponse:
    candidate = service.discover_candidate(
        source_document=_source_document(payload),
        provenance=payload.provenance,
        title=payload.title,
        description=payload.description,
        due_date=payload.due_date,
        target=payload.target,
        owner=None if payload.owner is None else payload.owner.to_domain(),
        supervisor=(
            None if payload.supervisor is None else payload.supervisor.to_domain()
        ),
        evidence_expectation=payload.evidence_expectation,
        classification=payload.classification,
        source_location=(
            None
            if payload.source_location is None
            else payload.source_location.to_domain()
        ),
        discovery_reason=payload.discovery_reason,
        confidence=payload.confidence,
    )
    return CandidateResponse.from_domain(candidate)


@router.get(
    "/candidates/{candidate_id}",
    response_model=CandidateResponse,
    responses=ERROR_RESPONSES,
)
def get_candidate(candidate_id: UUID, service: Service) -> CandidateResponse:
    return CandidateResponse.from_domain(service.get_candidate(candidate_id))


@router.post(
    "/candidates/{candidate_id}/revisions",
    response_model=CandidateResponse,
    responses=ERROR_RESPONSES,
)
def revise_candidate(
    candidate_id: UUID,
    payload: CandidateRevisionRequest,
    service: Service,
) -> CandidateResponse:
    revision = payload.model_dump(exclude_unset=True)
    if "owner" in revision:
        revision["owner"] = None if payload.owner is None else payload.owner.to_domain()
    if "supervisor" in revision:
        revision["supervisor"] = (
            None if payload.supervisor is None else payload.supervisor.to_domain()
        )
    candidate = service.revise_candidate(candidate_id, CandidateRevision(**revision))
    return CandidateResponse.from_domain(candidate)


@router.post(
    "/candidates/{candidate_id}/review",
    response_model=CandidateResponse,
    responses=ERROR_RESPONSES,
)
def review_candidate(
    candidate_id: UUID,
    payload: CandidateReviewBody,
    service: Service,
) -> CandidateResponse:
    candidate = service.review_candidate(
        candidate_id,
        payload.action,
        note=payload.note,
        reason=payload.reason,
    )
    return CandidateResponse.from_domain(candidate)


@router.post(
    "/human-added",
    response_model=LearningSignalResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
)
def record_human_added(
    payload: HumanAddedFeedbackRequest,
    service: Service,
) -> LearningSignalResponse:
    requirement = Requirement.create(
        title=payload.title,
        description=payload.description,
        owner=payload.owner.to_domain(),
        supervisor=(
            None if payload.supervisor is None else payload.supervisor.to_domain()
        ),
        evidence_expectation=payload.evidence_expectation,
        provenance=payload.provenance,
        due_date=payload.due_date,
        target=payload.target,
    )
    signal = service.record_human_added(
        source_document=_source_document(payload),
        requirement=requirement,
        source_location=(
            None
            if payload.source_location is None
            else payload.source_location.to_domain()
        ),
        reason=payload.reason,
    )
    return LearningSignalResponse.from_domain(signal)


@router.get(
    "/signals/{signal_id}",
    response_model=LearningSignalResponse,
    responses=ERROR_RESPONSES,
)
def get_signal(signal_id: UUID, service: Service) -> LearningSignalResponse:
    return LearningSignalResponse.from_domain(service.get_signal(signal_id))


def _source_document(
    payload: DiscoverCandidateRequest | HumanAddedFeedbackRequest,
) -> SourceDocument:
    return SourceDocument.create(
        filename=payload.source_filename,
        document_type=payload.source_document_type,
        source_reference=payload.source_reference,
    )
