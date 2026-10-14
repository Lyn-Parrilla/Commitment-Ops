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
from app.domain import (
    EvidenceStatus,
    EvidenceType,
    Person,
    Requirement,
    RequirementProvenance,
    RequirementStatus,
    ReviewStatus,
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
