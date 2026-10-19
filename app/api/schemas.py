"""Explicit HTTP request, response, and error contracts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.application import (
    AddRequirementCommand,
    RequirementReviewAction,
    RequirementTransition,
    WorkflowTransition,
)
from app.application.discovery import CandidateReviewAction
from app.domain.assistance import (
    ProposalDecision,
    ProposalEventType,
    ProposalState,
    RecommendationType,
)
from app.persistence.assistance_repository import StoredProposal
from app.domain import (
    DiscoveryClassification,
    DocumentType,
    EvidenceStatus,
    EvidenceType,
    HumanDecision,
    LearningSignal,
    Person,
    Requirement,
    RequirementCandidate,
    RequirementProvenance,
    RequirementStatus,
    ReviewStatus,
    SourceLocation,
    Workflow,
    WorkflowStatus,
    WorkflowType,
)


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PersonInput(APIModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=1, max_length=320)
    role: str | None = Field(default=None, max_length=200)

    @model_validator(mode="before")
    @classmethod
    def reject_client_person_id(cls, value: Any) -> Any:
        if isinstance(value, dict) and "id" in value:
            raise ValueError("person id is assigned by the server")
        return value

    def to_domain(self) -> Person:
        return Person.create(name=self.name, email=self.email, role=self.role)


class PersonResponse(APIModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    name: str
    email: str
    role: str | None


class EvidenceResponse(APIModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    description: str
    evidence_type: EvidenceType
    submitted_by: PersonResponse
    submitted_at: datetime
    reference: str | None
    status: EvidenceStatus


class RequirementResponse(APIModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    title: str
    description: str
    owner: PersonResponse
    evidence_expectation: str
    provenance: RequirementProvenance
    due_date: date | None
    target: str | None
    supervisor: PersonResponse | None
    status: RequirementStatus
    review_status: ReviewStatus
    reviewed_by: PersonResponse | None
    reviewed_at: datetime | None
    review_note: str | None
    evidence: list[EvidenceResponse]
    is_authoritative: bool

    @classmethod
    def from_domain(cls, requirement: Requirement) -> RequirementResponse:
        return cls.model_validate(requirement)


class WorkflowResponse(APIModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    organization_id: UUID
    name: str
    workflow_type: WorkflowType
    status: WorkflowStatus
    requirements: list[RequirementResponse]

    @classmethod
    def from_domain(cls, workflow: Workflow) -> WorkflowResponse:
        return cls.model_validate(workflow)


class CreateWorkflowRequest(APIModel):
    name: str = Field(min_length=1, max_length=300)
    workflow_type: WorkflowType


class AddRequirementRequest(APIModel):
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1)
    owner: PersonInput
    evidence_expectation: str = Field(min_length=1)
    provenance: RequirementProvenance
    due_date: date | None = None
    target: str | None = Field(default=None, max_length=500)
    supervisor: PersonInput | None = None

    def to_command(self) -> AddRequirementCommand:
        return AddRequirementCommand(
            title=self.title,
            description=self.description,
            owner=self.owner.to_domain(),
            supervisor=(
                self.supervisor.to_domain() if self.supervisor else None
            ),
            evidence_expectation=self.evidence_expectation,
            provenance=self.provenance,
            due_date=self.due_date,
            target=self.target,
        )


class WorkflowTransitionRequest(APIModel):
    action: WorkflowTransition


class RequirementTransitionRequest(APIModel):
    action: RequirementTransition


class RequirementReviewRequest(APIModel):
    action: RequirementReviewAction
    note: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def validate_action_fields(self) -> RequirementReviewRequest:
        if (
            self.action == RequirementReviewAction.REJECT
            and (self.reason is None or not self.reason.strip())
        ):
            raise ValueError("reason is required for rejection")
        if self.action == RequirementReviewAction.RESUBMIT and any(
            value is not None for value in (self.note, self.reason)
        ):
            raise ValueError("resubmit accepts only the action field")
        return self


class SourceLocationInput(APIModel):
    page: int | None = Field(default=None, ge=1)
    section: str | None = Field(default=None, max_length=500)
    heading: str | None = Field(default=None, max_length=500)
    paragraph: str | None = Field(default=None, max_length=500)
    table: str | None = Field(default=None, max_length=500)
    reference: str | None = Field(default=None, max_length=500)

    def to_domain(self) -> SourceLocation:
        return SourceLocation(
            page=self.page,
            section=self.section,
            heading=self.heading,
            paragraph=self.paragraph,
            table=self.table,
            reference=self.reference,
        )


class DiscoverCandidateRequest(APIModel):
    title: str | None = Field(default=None, max_length=500)
    description: str | None = None
    due_date: date | None = None
    target: str | None = Field(default=None, max_length=500)
    owner: PersonInput | None = None
    supervisor: PersonInput | None = None
    evidence_expectation: str | None = None
    provenance: RequirementProvenance
    classification: DiscoveryClassification = DiscoveryClassification.REQUIREMENT
    source_filename: str = Field(min_length=1, max_length=500)
    source_document_type: DocumentType
    source_reference: str = Field(min_length=1, max_length=1000)
    source_location: SourceLocationInput | None = None
    discovery_reason: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class CandidateRevisionRequest(APIModel):
    title: str | None = Field(default=None, max_length=500)
    description: str | None = None
    due_date: date | None = None
    target: str | None = Field(default=None, max_length=500)
    owner: PersonInput | None = None
    supervisor: PersonInput | None = None
    evidence_expectation: str | None = None
    classification: DiscoveryClassification | None = None


class CandidateReviewBody(APIModel):
    action: CandidateReviewAction
    note: str | None = None
    reason: str | None = None


class HumanAddedFeedbackRequest(APIModel):
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1)
    owner: PersonInput
    supervisor: PersonInput | None = None
    evidence_expectation: str = Field(min_length=1)
    provenance: RequirementProvenance
    due_date: date | None = None
    target: str | None = Field(default=None, max_length=500)
    source_filename: str = Field(min_length=1, max_length=500)
    source_document_type: DocumentType
    source_reference: str = Field(min_length=1, max_length=1000)
    source_location: SourceLocationInput | None = None
    reason: str | None = None


class SourceLocationResponse(APIModel):
    page: int | None
    section: str | None
    heading: str | None
    paragraph: str | None
    table: str | None
    reference: str | None

    @classmethod
    def from_domain(cls, location: SourceLocation) -> SourceLocationResponse:
        return cls(
            page=location.page,
            section=location.section,
            heading=location.heading,
            paragraph=location.paragraph,
            table=location.table,
            reference=location.reference,
        )


class LearningSignalResponse(APIModel):
    id: UUID
    decision_id: UUID
    candidate_id: UUID | None
    human_decision: HumanDecision
    reviewer: PersonResponse
    decision_reason: str | None
    decided_at: datetime
    source_document_id: UUID
    source_filename: str
    source_reference: str
    source_location: SourceLocationResponse | None
    original_title: str | None
    reviewed_title: str | None
    resulting_requirement_id: UUID | None
    resulting_provenance: RequirementProvenance | None

    @classmethod
    def from_domain(cls, signal: LearningSignal) -> LearningSignalResponse:
        original = signal.original_discovery
        reviewed = signal.reviewed_candidate
        result = signal.resulting_requirement
        return cls(
            id=signal.id,
            decision_id=signal.decision_id,
            candidate_id=signal.candidate_id,
            human_decision=signal.human_decision,
            reviewer=PersonResponse(
                id=signal.reviewer.id,
                name=signal.reviewer.name,
                email=signal.reviewer.email,
                role=signal.reviewer.role,
            ),
            decision_reason=signal.decision_reason,
            decided_at=signal.decided_at,
            source_document_id=signal.source_document.id,
            source_filename=signal.source_document.filename,
            source_reference=signal.source_document.source_reference,
            source_location=(
                None
                if signal.source_location is None
                else SourceLocationResponse.from_domain(signal.source_location)
            ),
            original_title=None if original is None else original.title,
            reviewed_title=None if reviewed is None else reviewed.title,
            resulting_requirement_id=None if result is None else result.requirement_id,
            resulting_provenance=None if result is None else result.provenance,
        )


class CandidateResponse(APIModel):
    id: UUID
    is_authoritative: bool
    provenance: RequirementProvenance
    classification: DiscoveryClassification
    title: str | None
    description: str | None
    review_status: ReviewStatus
    reviewed_by: PersonResponse | None
    reviewed_at: datetime | None
    review_note: str | None
    source_document_id: UUID
    source_filename: str
    source_reference: str
    source_location: SourceLocationResponse | None
    learning_signals: list[LearningSignalResponse]

    @classmethod
    def from_domain(cls, candidate: RequirementCandidate) -> CandidateResponse:
        location = candidate.source_location
        reviewer = candidate.reviewed_by
        return cls(
            id=candidate.id,
            is_authoritative=candidate.is_authoritative,
            provenance=candidate.provenance,
            classification=candidate.classification,
            title=candidate.title,
            description=candidate.description,
            review_status=candidate.review_status,
            reviewed_by=(
                None
                if reviewer is None
                else PersonResponse(
                    id=reviewer.id,
                    name=reviewer.name,
                    email=reviewer.email,
                    role=reviewer.role,
                )
            ),
            reviewed_at=candidate.reviewed_at,
            review_note=candidate.review_note,
            source_document_id=candidate.source_document_snapshot.id,
            source_filename=candidate.source_document_snapshot.filename,
            source_reference=candidate.source_document_snapshot.source_reference,
            source_location=(
                None
                if location is None
                else SourceLocationResponse.from_domain(location)
            ),
            learning_signals=[
                LearningSignalResponse.from_domain(signal)
                for signal in candidate.learning_signals
            ],
        )


class AssistanceDecisionRequest(APIModel):
    action: ProposalDecision


class AssistanceEventResponse(APIModel):
    id: UUID
    event_type: ProposalEventType
    actor_user_id: UUID
    occurred_at: datetime
    detail: dict[str, Any]


class AssistanceProposalResponse(APIModel):
    id: UUID
    organization_id: UUID
    workflow_id: UUID
    requirement_id: UUID
    recommendation_type: RecommendationType
    rationale: str
    intended_outcome: str
    state: ProposalState
    created_at: datetime
    events: list[AssistanceEventResponse]

    @classmethod
    def from_stored(cls, proposal: StoredProposal) -> AssistanceProposalResponse:
        return cls(
            id=proposal.id,
            organization_id=proposal.organization_id,
            workflow_id=proposal.workflow_id,
            requirement_id=proposal.requirement_id,
            recommendation_type=proposal.recommendation_type,
            rationale=proposal.rationale,
            intended_outcome=proposal.intended_outcome,
            state=proposal.state,
            created_at=proposal.created_at,
            events=[
                AssistanceEventResponse(
                    id=event.id,
                    event_type=event.event_type,
                    actor_user_id=event.actor_user_id,
                    occurred_at=event.occurred_at,
                    detail=event.detail,
                )
                for event in proposal.events
            ],
        )


class ValidationIssue(APIModel):
    location: str
    message: str
    type: str


class ErrorDetail(APIModel):
    code: str
    message: str
    details: list[ValidationIssue] | None = None


class ErrorResponse(APIModel):
    error: ErrorDetail


def validation_issues(errors: list[dict[str, Any]]) -> list[ValidationIssue]:
    return [
        ValidationIssue(
            location=".".join(str(part) for part in error["loc"]),
            message=error["msg"],
            type=error["type"],
        )
        for error in errors
    ]
