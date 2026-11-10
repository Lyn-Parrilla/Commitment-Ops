"""Authenticated grant and contract intake. Review stays a human decision."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse

from app.api.bounded_multipart import OversizedUpload, read_bounded_multipart
from app.api.dependencies import get_document_intake_service
from app.api.schemas import (
    CandidateResponse,
    CandidateReviewBody,
    CandidateRevisionRequest,
    ErrorResponse,
    MissingRequirementRequest,
    RequirementResponse,
    SourceDocumentResponse,
    SourceDocumentUploadResponse,
    SourceExcerptResponse,
)
from app.application.document_intake import DocumentIntakeService
from app.domain import CandidateRevision, DocumentType
from app.domain.evidence_files import inspect_source_document
from app.domain.exceptions import DomainValidationError
from app.persistence.source_documents import StoredSourceDocument

router = APIRouter(prefix="/api/v1/workflows", tags=["source-documents"])
Service = Annotated[DocumentIntakeService, Depends(get_document_intake_service)]
_TEXT_FIELDS = frozenset(
    {"description", "reference", "submitter_name", "document_type"}
)
ERROR_RESPONSES = {
    404: {"model": ErrorResponse, "description": "Resource not found"},
    409: {"model": ErrorResponse, "description": "Domain state conflict"},
    422: {"model": ErrorResponse, "description": "Invalid request or domain data"},
    503: {"model": ErrorResponse, "description": "Persistence unavailable"},
}


@router.post(
    "/{workflow_id}/source-documents",
    response_model=SourceDocumentUploadResponse,
    responses=ERROR_RESPONSES,
)
async def upload_source_document(
    workflow_id: UUID,
    request: Request,
    service: Service,
) -> JSONResponse:
    try:
        form = await read_bounded_multipart(request, text_fields=_TEXT_FIELDS)
    except OversizedUpload as exc:
        raise DomainValidationError("This file is too large.") from exc
    if not form.content:
        raise DomainValidationError("A document file is required.")
    inspected = inspect_source_document(form.filename, form.content)
    raw_type = (form.fields.get("document_type") or "grant_agreement").strip()
    try:
        document_type = DocumentType(raw_type)
    except ValueError as exc:
        raise DomainValidationError("This document type is not supported.") from exc
    uploaded = service.upload(workflow_id, inspected, document_type)
    body = SourceDocumentUploadResponse(
        document=_document_response(uploaded.document),
        duplicate=uploaded.duplicate,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK if uploaded.duplicate else status.HTTP_201_CREATED,
        content=body.model_dump(mode="json"),
    )


@router.get(
    "/{workflow_id}/source-documents",
    response_model=list[SourceDocumentResponse],
    responses=ERROR_RESPONSES,
)
def list_source_documents(
    workflow_id: UUID,
    service: Service,
) -> list[SourceDocumentResponse]:
    return [
        _document_response(document)
        for document in service.list_documents(workflow_id)
    ]


@router.get(
    "/{workflow_id}/source-documents/{document_id}",
    response_model=SourceDocumentResponse,
    responses=ERROR_RESPONSES,
)
def get_source_document(
    workflow_id: UUID,
    document_id: UUID,
    service: Service,
) -> SourceDocumentResponse:
    return _document_response(service.get_document(workflow_id, document_id))


@router.post(
    "/{workflow_id}/source-documents/{document_id}/process",
    response_model=SourceDocumentResponse,
    responses=ERROR_RESPONSES,
)
def process_source_document(
    workflow_id: UUID,
    document_id: UUID,
    service: Service,
) -> SourceDocumentResponse:
    return _document_response(service.process(workflow_id, document_id))


@router.get(
    "/{workflow_id}/source-documents/{document_id}/excerpt",
    response_model=SourceExcerptResponse,
    responses=ERROR_RESPONSES,
)
def source_excerpt(
    workflow_id: UUID,
    document_id: UUID,
    service: Service,
    page: Annotated[int | None, Query(ge=1)] = None,
    section: Annotated[str | None, Query(max_length=200)] = None,
) -> SourceExcerptResponse:
    excerpt = service.excerpt(
        workflow_id,
        document_id,
        page=page,
        section=section,
    )
    return SourceExcerptResponse(
        document_id=document_id,
        page=excerpt.page,
        text=excerpt.text,
    )


@router.get(
    "/{workflow_id}/source-documents/{document_id}/candidates",
    response_model=list[CandidateResponse],
    responses=ERROR_RESPONSES,
)
def list_document_candidates(
    workflow_id: UUID,
    document_id: UUID,
    service: Service,
) -> list[CandidateResponse]:
    return [
        CandidateResponse.from_domain(candidate)
        for candidate in service.candidates(workflow_id, document_id)
    ]


@router.post(
    "/{workflow_id}/source-documents/{document_id}/candidates/{candidate_id}/revisions",
    response_model=CandidateResponse,
    responses=ERROR_RESPONSES,
)
def revise_document_candidate(
    workflow_id: UUID,
    document_id: UUID,
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
    candidate = service.revise_candidate(
        workflow_id,
        document_id,
        candidate_id,
        CandidateRevision(**revision),
    )
    return CandidateResponse.from_domain(candidate)


@router.post(
    "/{workflow_id}/source-documents/{document_id}/candidates/{candidate_id}/review",
    response_model=CandidateResponse,
    responses=ERROR_RESPONSES,
)
def review_document_candidate(
    workflow_id: UUID,
    document_id: UUID,
    candidate_id: UUID,
    payload: CandidateReviewBody,
    service: Service,
) -> CandidateResponse:
    candidate = service.review_candidate(
        workflow_id,
        document_id,
        candidate_id,
        payload.action,
        note=payload.note,
        reason=payload.reason,
    )
    return CandidateResponse.from_domain(candidate)


@router.post(
    "/{workflow_id}/source-documents/{document_id}/requirements",
    response_model=RequirementResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
)
def add_missing_requirement(
    workflow_id: UUID,
    document_id: UUID,
    payload: MissingRequirementRequest,
    service: Service,
) -> RequirementResponse:
    requirement = service.add_missing_requirement(
        workflow_id,
        document_id,
        title=payload.title,
        description=payload.description,
        owner=payload.owner.to_domain(),
        supervisor=(
            None if payload.supervisor is None else payload.supervisor.to_domain()
        ),
        evidence_expectation=payload.evidence_expectation,
        due_date=payload.due_date,
        target=payload.target,
        source_location=(
            None
            if payload.source_location is None
            else payload.source_location.to_domain()
        ),
        reason=payload.reason,
    )
    return RequirementResponse.from_domain(requirement)


def _document_response(document: StoredSourceDocument) -> SourceDocumentResponse:
    return SourceDocumentResponse(
        id=document.id,
        workflow_id=document.workflow_id,
        filename=document.filename,
        media_type=document.media_type,
        byte_size=document.byte_size,
        document_type=document.document_type,
        processing_status=document.processing_status,
        failure_code=document.failure_code,
        text_truncated=document.text_truncated,
        uploaded_at=document.uploaded_at,
    )
