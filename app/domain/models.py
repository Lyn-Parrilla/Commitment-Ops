"""In-memory domain entities for people, evidence, requirements, and workflows."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import (
    EvidenceStatus,
    EvidenceType,
    RequirementProvenance,
    RequirementStatus,
    ReviewStatus,
    WorkflowStatus,
    WorkflowType,
)
from app.domain.exceptions import DomainValidationError, InvalidTransitionError


def _require_text(value: str | None, field_name: str) -> str:
    if value is None or not value.strip():
        raise DomainValidationError(f"{field_name} is required")
    return value.strip()


def _require_email(value: str) -> str:
    email = _require_text(value, "email")
    local, separator, domain = email.partition("@")
    if (
        separator != "@"
        or not local
        or "." not in domain
        or domain.startswith(".")
        or domain.endswith(".")
        or " " in email
    ):
        raise DomainValidationError("email must be a valid address")
    return email


class Person(BaseModel):
    """Accountable person: owner, supervisor, reviewer, or submitter."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    name: str
    email: str
    role: str | None = None

    @classmethod
    def create(cls, *, name: str, email: str, role: str | None = None) -> Person:
        cleaned_role = role.strip() if role and role.strip() else None
        return cls(name=_require_text(name, "name"), email=_require_email(email), role=cleaned_role)


class Evidence(BaseModel):
    """Proof offered against a requirement. Storage and files are out of scope."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    description: str
    evidence_type: EvidenceType
    submitted_by: Person
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reference: str | None = None
    status: EvidenceStatus = EvidenceStatus.SUBMITTED

    @classmethod
    def create(
        cls,
        *,
        description: str,
        evidence_type: EvidenceType,
        submitted_by: Person,
        reference: str | None = None,
        submitted_at: datetime | None = None,
    ) -> Evidence:
        cleaned_reference = reference.strip() if reference and reference.strip() else None
        return cls(
            description=_require_text(description, "description"),
            evidence_type=evidence_type,
            submitted_by=submitted_by,
            reference=cleaned_reference,
            submitted_at=submitted_at or datetime.now(UTC),
        )


class Requirement(BaseModel):
    """Something the organization must accomplish, with owner, due date, and evidence."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    title: str
    description: str
    owner: Person
    evidence_expectation: str
    provenance: RequirementProvenance
    due_date: date | None = None
    target: str | None = None
    supervisor: Person | None = None
    status: RequirementStatus = RequirementStatus.NOT_STARTED
    review_status: ReviewStatus = ReviewStatus.PENDING_REVIEW
    reviewed_by: Person | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)

    @property
    def is_authoritative(self) -> bool:
        return self.review_status == ReviewStatus.APPROVED

    @classmethod
    def create(
        cls,
        *,
        title: str,
        description: str,
        owner: Person,
        evidence_expectation: str,
        provenance: RequirementProvenance,
        due_date: date | None = None,
        target: str | None = None,
        supervisor: Person | None = None,
        review_status: ReviewStatus | None = None,
    ) -> Requirement:
        if owner is None:
            raise DomainValidationError("owner is required")

        initial_review = _initial_review_status(provenance, review_status)
        cleaned_target = target.strip() if target and target.strip() else None

        return cls(
            title=_require_text(title, "title"),
            description=_require_text(description, "description"),
            owner=owner,
            evidence_expectation=_require_text(evidence_expectation, "evidence_expectation"),
            provenance=provenance,
            due_date=due_date,
            target=cleaned_target,
            supervisor=supervisor,
            review_status=initial_review,
        )

    def assign_owner(self, owner: Person) -> None:
        if owner is None:
            raise DomainValidationError("owner is required")
        if self.status in {RequirementStatus.COMPLETED, RequirementStatus.CANCELLED}:
            raise InvalidTransitionError("cannot reassign a closed requirement")
        self.owner = owner

    def assign_supervisor(self, supervisor: Person | None) -> None:
        self.supervisor = supervisor

    def submit_for_review(self) -> None:
        if self.review_status != ReviewStatus.REJECTED:
            raise InvalidTransitionError(
                f"cannot submit for review from {self.review_status}"
            )
        self.review_status = ReviewStatus.PENDING_REVIEW
        self.reviewed_by = None
        self.reviewed_at = None
        self.review_note = None

    def approve(self, *, reviewer: Person, note: str | None = None) -> None:
        if self.review_status != ReviewStatus.PENDING_REVIEW:
            raise InvalidTransitionError(
                f"cannot approve a requirement in review status {self.review_status}"
            )
        self.review_status = ReviewStatus.APPROVED
        self.reviewed_by = reviewer
        self.reviewed_at = datetime.now(UTC)
        self.review_note = note.strip() if note and note.strip() else None

    def reject(self, *, reviewer: Person, reason: str) -> None:
        if self.review_status != ReviewStatus.PENDING_REVIEW:
            raise InvalidTransitionError(
                f"cannot reject a requirement in review status {self.review_status}"
            )
        self.review_status = ReviewStatus.REJECTED
        self.reviewed_by = reviewer
        self.reviewed_at = datetime.now(UTC)
        self.review_note = _require_text(reason, "reason")

    def start_work(self) -> None:
        self._require_authoritative()
        self._set_status(
            allowed={RequirementStatus.NOT_STARTED, RequirementStatus.BLOCKED},
            new_status=RequirementStatus.IN_PROGRESS,
        )

    def block(self) -> None:
        self._require_authoritative()
        self._set_status(
            allowed={RequirementStatus.IN_PROGRESS},
            new_status=RequirementStatus.BLOCKED,
        )

    def complete(self) -> None:
        self._require_authoritative()
        self._set_status(
            allowed={RequirementStatus.NOT_STARTED, RequirementStatus.IN_PROGRESS},
            new_status=RequirementStatus.COMPLETED,
        )

    def cancel(self) -> None:
        self._set_status(
            allowed={
                RequirementStatus.NOT_STARTED,
                RequirementStatus.IN_PROGRESS,
                RequirementStatus.BLOCKED,
            },
            new_status=RequirementStatus.CANCELLED,
        )

    def add_evidence(self, evidence: Evidence) -> None:
        if self.status == RequirementStatus.CANCELLED:
            raise InvalidTransitionError("cannot attach evidence to a cancelled requirement")
        if any(item.id == evidence.id for item in self.evidence):
            raise DomainValidationError("evidence is already associated with this requirement")
        self.evidence.append(evidence)

    def _require_authoritative(self) -> None:
        if not self.is_authoritative:
            raise InvalidTransitionError(
                "requirement must be approved before work status can change"
            )

    def _set_status(
        self, *, allowed: set[RequirementStatus], new_status: RequirementStatus
    ) -> None:
        if self.status not in allowed:
            raise InvalidTransitionError(
                f"cannot change requirement status from {self.status} to {new_status}"
            )
        self.status = new_status


def _initial_review_status(
    provenance: RequirementProvenance,
    requested: ReviewStatus | None,
) -> ReviewStatus:
    if provenance == RequirementProvenance.AI_INFERRED:
        if requested == ReviewStatus.APPROVED:
            raise DomainValidationError(
                "AI-inferred requirements cannot be created as authoritative"
            )
        return requested or ReviewStatus.PENDING_REVIEW
    return requested or ReviewStatus.APPROVED


class Workflow(BaseModel):
    """Accountability workflow that groups requirements under a named effort."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    name: str
    workflow_type: WorkflowType
    status: WorkflowStatus = WorkflowStatus.DRAFT
    requirements: list[Requirement] = Field(default_factory=list)

    @classmethod
    def create(cls, *, name: str, workflow_type: WorkflowType) -> Workflow:
        return cls(name=_require_text(name, "name"), workflow_type=workflow_type)

    def add_requirement(self, requirement: Requirement) -> None:
        if self.status in {WorkflowStatus.COMPLETED, WorkflowStatus.CANCELLED}:
            raise InvalidTransitionError("cannot add requirements to a closed workflow")
        if any(item.id == requirement.id for item in self.requirements):
            raise DomainValidationError("requirement is already on this workflow")
        self.requirements.append(requirement)

    def activate(self) -> None:
        if self.status != WorkflowStatus.DRAFT:
            raise InvalidTransitionError(
                f"cannot activate a workflow in status {self.status}"
            )
        self.status = WorkflowStatus.ACTIVE

    def complete(self) -> None:
        if self.status != WorkflowStatus.ACTIVE:
            raise InvalidTransitionError(
                f"cannot complete a workflow in status {self.status}"
            )
        self.status = WorkflowStatus.COMPLETED

    def cancel(self) -> None:
        if self.status not in {WorkflowStatus.DRAFT, WorkflowStatus.ACTIVE}:
            raise InvalidTransitionError(
                f"cannot cancel a workflow in status {self.status}"
            )
        self.status = WorkflowStatus.CANCELLED
