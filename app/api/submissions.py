"""Member submission links and the unauthenticated submission action."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import Field, ValidationError

from app.api.dependencies import (
    get_submission_repository,
    get_submission_service,
)
from app.api.schemas import APIModel, ErrorResponse
from app.application.exceptions import SubmissionLinkUnavailableError
from app.api.bounded_multipart import OversizedUpload, read_bounded_multipart
from app.domain.evidence_files import inspect_evidence_file
from app.domain.exceptions import DomainValidationError
from app.application.submissions import IssuedSubmissionLink, SubmissionService
from app.persistence.submissions import (
    PublicSubmissionContext,
    StoredSubmissionLink,
    SubmissionRepository,
)

router = APIRouter(prefix="/api/v1", tags=["submissions"])
Service = Annotated[SubmissionService, Depends(get_submission_service)]
Repository = Annotated[SubmissionRepository, Depends(get_submission_repository)]
SubmissionToken = Annotated[
    str | None,
    Header(alias="X-Submission-Token"),
]

ERROR_RESPONSES = {
    404: {"model": ErrorResponse, "description": "Resource not found"},
    409: {"model": ErrorResponse, "description": "Domain state conflict"},
    422: {"model": ErrorResponse, "description": "Invalid request or domain data"},
    503: {"model": ErrorResponse, "description": "Persistence unavailable"},
}


class SubmissionLinkResponse(APIModel):
    id: UUID
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    max_uses: int
    use_count: int
    last_used_at: datetime | None

    @classmethod
    def from_stored(cls, link: StoredSubmissionLink) -> SubmissionLinkResponse:
        return cls(
            id=link.id,
            created_at=link.created_at,
            expires_at=link.expires_at,
            revoked_at=link.revoked_at,
            max_uses=link.max_uses,
            use_count=link.use_count,
            last_used_at=link.last_used_at,
        )


class IssuedSubmissionLinkResponse(SubmissionLinkResponse):
    submission_path: str

    @classmethod
    def from_issued(cls, issued: IssuedSubmissionLink) -> IssuedSubmissionLinkResponse:
        body = SubmissionLinkResponse.from_stored(issued.link).model_dump()
        return cls(**body, submission_path=issued.submission_path)


class PublicSubmissionContextResponse(APIModel):
    organization_name: str
    requirement_title: str
    evidence_expectation: str

    @classmethod
    def from_context(
        cls,
        context: PublicSubmissionContext,
    ) -> PublicSubmissionContextResponse:
        return cls(
            organization_name=context.organization_name,
            requirement_title=context.requirement_title,
            evidence_expectation=context.evidence_expectation,
        )


class PublicSubmissionRequest(APIModel):
    description: str = Field(min_length=1, max_length=4000)
    reference: str = Field(min_length=1, max_length=1000)
    submitter_name: str | None = Field(default=None, max_length=200)


class PublicSubmissionResponse(APIModel):
    status: Literal["submitted"]


def _require_context(context: PublicSubmissionContext | None) -> PublicSubmissionContext:
    if context is None:
        raise SubmissionLinkUnavailableError
    return context


@router.post(
    "/workflows/{workflow_id}/requirements/{requirement_id}/submission-links",
    response_model=IssuedSubmissionLinkResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
)
def create_submission_link(
    workflow_id: UUID,
    requirement_id: UUID,
    service: Service,
) -> IssuedSubmissionLinkResponse:
    return IssuedSubmissionLinkResponse.from_issued(
        service.create_link(workflow_id, requirement_id)
    )


@router.get(
    "/workflows/{workflow_id}/requirements/{requirement_id}/submission-links",
    response_model=list[SubmissionLinkResponse],
    responses=ERROR_RESPONSES,
)
def list_submission_links(
    workflow_id: UUID,
    requirement_id: UUID,
    service: Service,
) -> list[SubmissionLinkResponse]:
    return [
        SubmissionLinkResponse.from_stored(link)
        for link in service.list_links(workflow_id, requirement_id)
    ]


@router.post(
    "/workflows/{workflow_id}/requirements/{requirement_id}/submission-links/{link_id}/revoke",
    response_model=SubmissionLinkResponse,
    responses=ERROR_RESPONSES,
)
def revoke_submission_link(
    workflow_id: UUID,
    requirement_id: UUID,
    link_id: UUID,
    service: Service,
) -> SubmissionLinkResponse:
    return SubmissionLinkResponse.from_stored(
        service.revoke_link(workflow_id, requirement_id, link_id)
    )


@router.get(
    "/submission-context",
    response_model=PublicSubmissionContextResponse,
    responses=ERROR_RESPONSES,
)
def submission_context(
    repository: Repository,
    submission_token: SubmissionToken = None,
) -> PublicSubmissionContextResponse:
    context = _require_context(repository.public_context(submission_token))
    return PublicSubmissionContextResponse.from_context(context)


@router.post(
    "/submissions",
    response_model=PublicSubmissionResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
)
async def submit_public_evidence(
    request: Request,
    repository: Repository,
    submission_token: SubmissionToken = None,
) -> PublicSubmissionResponse:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        evidence_id = await _submit_multipart(
            request,
            repository,
            submission_token,
        )
    else:
        try:
            payload = PublicSubmissionRequest.model_validate_json(await request.body())
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from None
        evidence_id = repository.public_submit(
            submission_token,
            description=payload.description,
            reference=payload.reference,
            submitter_name=payload.submitter_name,
        )
    if evidence_id is None:
        raise SubmissionLinkUnavailableError
    return PublicSubmissionResponse(status="submitted")


@router.get(
    "/workflows/{workflow_id}/requirements/{requirement_id}/evidence/{evidence_id}/file",
    responses=ERROR_RESPONSES,
)
def download_evidence_file(
    workflow_id: UUID,
    requirement_id: UUID,
    evidence_id: UUID,
    service: Service,
) -> None:
    """Member download. Unscanned files are refused and no object URL is returned."""

    service.evidence_file_status(workflow_id, requirement_id, evidence_id)


async def _submit_multipart(
    request: Request,
    repository: SubmissionRepository,
    submission_token: str | None,
) -> UUID | None:
    try:
        form = await read_bounded_multipart(request)
    except OversizedUpload as exc:
        raise DomainValidationError("This file is too large.") from exc
    description = form.fields.get("description") or None
    reference = form.fields.get("reference") or None
    submitter_name = form.fields.get("submitter_name") or None
    if form.content:
        inspected = inspect_evidence_file(form.filename, form.content)
        return repository.public_submit_file(
            submission_token,
            description=description or "",
            reference=reference,
            submitter_name=submitter_name,
            inspected=inspected,
        )
    if reference is None:
        raise DomainValidationError("The submission is not valid.")
    return repository.public_submit(
        submission_token,
        description=description or "",
        reference=reference,
        submitter_name=submitter_name,
    )
