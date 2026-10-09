"""Deterministic intake, discovery review, and controlled-learning contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import RequirementProvenance, ReviewStatus
from app.domain.exceptions import DomainValidationError, InvalidTransitionError
from app.domain.models import Person, Requirement, _require_text


class DocumentType(StrEnum):
    GRANT_AGREEMENT = "grant_agreement"
    CONTRACT = "contract"
    POLICY = "policy"
    REGULATORY_FILING = "regulatory_filing"
    IMPLEMENTATION_PLAN = "implementation_plan"
    OTHER = "other"


class DocumentProcessingStatus(StrEnum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class DiscoveryClassification(StrEnum):
    REQUIREMENT = "requirement"
    DELIVERABLE = "deliverable"


class AccountabilityField(StrEnum):
    TITLE = "title"
    DESCRIPTION = "description"
    OWNER = "owner"
    EVIDENCE_EXPECTATION = "evidence_expectation"
    DUE_DATE = "due_date"
    TARGET = "target"
    SUPERVISOR = "supervisor"


class FieldPresence(StrEnum):
    PRESENT = "present"
    REQUIRED_MISSING = "required_missing"
    OPTIONAL_MISSING = "optional_missing"


class HumanDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    HUMAN_ADDED = "human_added"


class SourceLocation(BaseModel):
    """Known location within a source. Unavailable details remain unset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page: int | None = Field(default=None, ge=1)
    section: str | None = None
    heading: str | None = None
    paragraph: str | None = None
    table: str | None = None
    reference: str | None = None

    @model_validator(mode="after")
    def require_known_location(self) -> SourceLocation:
        values = (
            self.page,
            self.section,
            self.heading,
            self.paragraph,
            self.table,
            self.reference,
        )
        if not any(value is not None and value != "" for value in values):
            raise ValueError("at least one source location detail is required")
        return self


class SourceDocument(BaseModel):
    """Traceable source material without a production storage dependency."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    filename: str
    document_type: DocumentType
    source_reference: str
    processing_status: DocumentProcessingStatus = DocumentProcessingStatus.UPLOADED
    uploaded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    organization_reference: str | None = None

    @classmethod
    def create(
        cls,
        *,
        filename: str,
        document_type: DocumentType,
        source_reference: str,
        uploaded_at: datetime | None = None,
        organization_reference: str | None = None,
    ) -> SourceDocument:
        organization = (
            organization_reference.strip()
            if organization_reference and organization_reference.strip()
            else None
        )
        return cls(
            filename=_require_text(filename, "filename"),
            document_type=document_type,
            source_reference=_require_text(source_reference, "source_reference"),
            uploaded_at=uploaded_at or datetime.now(UTC),
            organization_reference=organization,
        )

    def start_processing(self) -> None:
        if self.processing_status not in {
            DocumentProcessingStatus.UPLOADED,
            DocumentProcessingStatus.FAILED,
        }:
            raise InvalidTransitionError(
                f"cannot start processing from {self.processing_status}"
            )
        self.processing_status = DocumentProcessingStatus.PROCESSING

    def mark_processed(self) -> None:
        self._finish_processing(DocumentProcessingStatus.PROCESSED)

    def mark_failed(self) -> None:
        self._finish_processing(DocumentProcessingStatus.FAILED)

    def _finish_processing(self, status: DocumentProcessingStatus) -> None:
        if self.processing_status != DocumentProcessingStatus.PROCESSING:
            raise InvalidTransitionError(
                f"cannot change document status from {self.processing_status} to {status}"
            )
        self.processing_status = status


class PersonSnapshot(BaseModel):
    """Immutable person facts as known when a discovery decision was recorded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    name: str
    email: str
    role: str | None

    @classmethod
    def from_person(cls, person: Person) -> PersonSnapshot:
        return cls(
            id=person.id,
            name=person.name,
            email=person.email,
            role=person.role,
        )


class SourceDocumentSnapshot(BaseModel):
    """Immutable source metadata captured for discovery traceability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    filename: str
    document_type: DocumentType
    source_reference: str
    processing_status: DocumentProcessingStatus
    uploaded_at: datetime
    organization_reference: str | None

    @classmethod
    def from_document(cls, document: SourceDocument) -> SourceDocumentSnapshot:
        return cls(
            id=document.id,
            filename=document.filename,
            document_type=document.document_type,
            source_reference=document.source_reference,
            processing_status=document.processing_status,
            uploaded_at=document.uploaded_at,
            organization_reference=document.organization_reference,
        )


class FieldAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: AccountabilityField
    presence: FieldPresence


class CompletenessAssessment(BaseModel):
    """Deterministic required-versus-optional field assessment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fields: tuple[FieldAssessment, ...]

    @property
    def is_complete(self) -> bool:
        return not self.required_missing

    @property
    def required_missing(self) -> tuple[AccountabilityField, ...]:
        return tuple(
            item.field
            for item in self.fields
            if item.presence == FieldPresence.REQUIRED_MISSING
        )

    @property
    def optional_missing(self) -> tuple[AccountabilityField, ...]:
        return tuple(
            item.field
            for item in self.fields
            if item.presence == FieldPresence.OPTIONAL_MISSING
        )

    def presence_of(self, field: AccountabilityField) -> FieldPresence:
        for item in self.fields:
            if item.field == field:
                return item.presence
        raise KeyError(field)


class CandidateSnapshot(BaseModel):
    """Immutable discovery or reviewed-state snapshot used for comparison."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str | None
    description: str | None
    due_date: date | None
    target: str | None
    owner: PersonSnapshot | None
    supervisor: PersonSnapshot | None
    evidence_expectation: str | None
    provenance: RequirementProvenance
    classification: DiscoveryClassification
    source_document_id: UUID
    source_location: SourceLocation | None
    discovery_reason: str | None
    confidence: float | None = Field(default=None, ge=0, le=1)


class RequirementResultSnapshot(BaseModel):
    """Characteristics validated into authoritative requirement state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_id: UUID
    title: str
    description: str
    due_date: date | None
    target: str | None
    owner: PersonSnapshot
    supervisor: PersonSnapshot | None
    evidence_expectation: str
    provenance: RequirementProvenance

    @classmethod
    def from_requirement(
        cls, requirement: Requirement
    ) -> RequirementResultSnapshot:
        return cls(
            requirement_id=requirement.id,
            title=requirement.title,
            description=requirement.description,
            due_date=requirement.due_date,
            target=requirement.target,
            owner=PersonSnapshot.from_person(requirement.owner),
            supervisor=(
                PersonSnapshot.from_person(requirement.supervisor)
                if requirement.supervisor
                else None
            ),
            evidence_expectation=requirement.evidence_expectation,
            provenance=requirement.provenance,
        )


class LearningSignal(BaseModel):
    """Validated feedback; records performance but grants no authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    candidate_id: UUID | None
    source_document: SourceDocumentSnapshot
    source_location: SourceLocation | None
    original_discovery: CandidateSnapshot | None
    reviewed_candidate: CandidateSnapshot | None
    discovery_provenance: RequirementProvenance | None
    discovery_reason: str | None
    original_confidence: float | None = Field(default=None, ge=0, le=1)
    human_decision: HumanDecision
    reviewer: PersonSnapshot
    decision_reason: str | None
    resulting_requirement: RequirementResultSnapshot | None
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def corrected_fields(self) -> tuple[str, ...]:
        if self.original_discovery is None or self.reviewed_candidate is None:
            return ()
        before = self.original_discovery.model_dump()
        after = self.reviewed_candidate.model_dump()
        return tuple(
            field
            for field in CandidateSnapshot.model_fields
            if before[field] != after[field]
        )

    @property
    def is_human_correction(self) -> bool:
        return bool(self.corrected_fields)

    @classmethod
    def record_human_added(
        cls,
        *,
        source_document: SourceDocument,
        requirement: Requirement,
        reviewer: Person,
        source_location: SourceLocation | None = None,
        reason: str | None = None,
        decided_at: datetime | None = None,
    ) -> LearningSignal:
        if reviewer is None:
            raise DomainValidationError("reviewer is required")
        if not requirement.is_authoritative:
            raise DomainValidationError(
                "human-added learning requires an authoritative requirement"
            )
        return cls(
            candidate_id=None,
            source_document=SourceDocumentSnapshot.from_document(source_document),
            source_location=source_location,
            original_discovery=None,
            reviewed_candidate=None,
            discovery_provenance=None,
            discovery_reason=None,
            original_confidence=None,
            human_decision=HumanDecision.HUMAN_ADDED,
            reviewer=PersonSnapshot.from_person(reviewer),
            decision_reason=reason.strip() if reason and reason.strip() else None,
            resulting_requirement=RequirementResultSnapshot.from_requirement(
                requirement
            ),
            decided_at=decided_at or datetime.now(UTC),
        )


class CandidateRevision(BaseModel):
    """Human corrections to discovered accountability fields."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str | None = None
    due_date: date | None = None
    target: str | None = None
    owner: Person | None = None
    supervisor: Person | None = None
    evidence_expectation: str | None = None
    classification: DiscoveryClassification | None = None


class CandidateApproval(BaseModel):
    """Result of the explicit authority-boundary transition."""

    model_config = ConfigDict(extra="forbid")

    requirement: Requirement
    learning_signal: LearningSignal


class RequirementCandidate(BaseModel):
    """Potential requirement under review; never authoritative by itself."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    title: str | None = None
    description: str | None = None
    due_date: date | None = None
    target: str | None = None
    owner: Person | None = None
    supervisor: Person | None = None
    evidence_expectation: str | None = None
    provenance: RequirementProvenance
    classification: DiscoveryClassification = DiscoveryClassification.REQUIREMENT
    source_document: SourceDocument
    source_document_snapshot: SourceDocumentSnapshot
    source_location: SourceLocation | None = None
    discovery_reason: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    review_status: ReviewStatus = ReviewStatus.PENDING_REVIEW
    reviewed_by: Person | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None
    original_discovery: CandidateSnapshot
    learning_signals: list[LearningSignal] = Field(default_factory=list)

    @property
    def is_authoritative(self) -> bool:
        return False

    @classmethod
    def discover(
        cls,
        *,
        source_document: SourceDocument,
        provenance: RequirementProvenance,
        title: str | None = None,
        description: str | None = None,
        due_date: date | None = None,
        target: str | None = None,
        owner: Person | None = None,
        supervisor: Person | None = None,
        evidence_expectation: str | None = None,
        classification: DiscoveryClassification = DiscoveryClassification.REQUIREMENT,
        source_location: SourceLocation | None = None,
        discovery_reason: str | None = None,
        confidence: float | None = None,
    ) -> RequirementCandidate:
        values: dict[str, Any] = {
            "title": _clean_optional_text(title),
            "description": _clean_optional_text(description),
            "due_date": due_date,
            "target": _clean_optional_text(target),
            "owner": PersonSnapshot.from_person(owner) if owner else None,
            "supervisor": (
                PersonSnapshot.from_person(supervisor) if supervisor else None
            ),
            "evidence_expectation": _clean_optional_text(evidence_expectation),
            "provenance": provenance,
            "classification": classification,
            "source_document_id": source_document.id,
            "source_location": source_location,
            "discovery_reason": _clean_optional_text(discovery_reason),
            "confidence": confidence,
        }
        original = CandidateSnapshot(**values)
        candidate_values = {
            **{
                key: value
                for key, value in values.items()
                if key != "source_document_id"
            },
            "owner": owner,
            "supervisor": supervisor,
        }
        return cls(
            **{
                key: value for key, value in candidate_values.items()
            },
            source_document=source_document,
            source_document_snapshot=SourceDocumentSnapshot.from_document(
                source_document
            ),
            original_discovery=original.model_copy(deep=True),
        )

    def assess_completeness(self) -> CompletenessAssessment:
        required = {
            AccountabilityField.TITLE: self.title,
            AccountabilityField.DESCRIPTION: self.description,
            AccountabilityField.OWNER: self.owner,
            AccountabilityField.EVIDENCE_EXPECTATION: self.evidence_expectation,
        }
        optional = {
            AccountabilityField.DUE_DATE: self.due_date,
            AccountabilityField.TARGET: self.target,
            AccountabilityField.SUPERVISOR: self.supervisor,
        }
        fields = tuple(
            FieldAssessment(
                field=field,
                presence=(
                    FieldPresence.PRESENT
                    if _is_present(value)
                    else FieldPresence.REQUIRED_MISSING
                ),
            )
            for field, value in required.items()
        ) + tuple(
            FieldAssessment(
                field=field,
                presence=(
                    FieldPresence.PRESENT
                    if _is_present(value)
                    else FieldPresence.OPTIONAL_MISSING
                ),
            )
            for field, value in optional.items()
        )
        return CompletenessAssessment(fields=fields)

    def revise(self, revision: CandidateRevision) -> None:
        if self.review_status == ReviewStatus.APPROVED:
            raise InvalidTransitionError("cannot revise an approved candidate")
        for field in revision.model_fields_set:
            value = getattr(revision, field)
            if field in {"title", "description", "target", "evidence_expectation"}:
                value = _clean_optional_text(value)
            setattr(self, field, value)
        self.review_status = ReviewStatus.PENDING_REVIEW
        self.reviewed_by = None
        self.reviewed_at = None
        self.review_note = None

    def approve(
        self, *, reviewer: Person, note: str | None = None
    ) -> CandidateApproval:
        if reviewer is None:
            raise DomainValidationError("reviewer is required")
        self._require_pending_review("approve")
        completeness = self.assess_completeness()
        if not completeness.is_complete:
            missing = ", ".join(field.value for field in completeness.required_missing)
            raise DomainValidationError(
                f"candidate is missing required information: {missing}"
            )

        requirement = Requirement.create(
            title=self.title or "",
            description=self.description or "",
            owner=self.owner,  # type: ignore[arg-type]
            supervisor=self.supervisor,
            evidence_expectation=self.evidence_expectation or "",
            provenance=self.provenance,
            due_date=self.due_date,
            target=self.target,
            review_status=ReviewStatus.PENDING_REVIEW,
        )
        requirement.approve(reviewer=reviewer, note=note)

        self.review_status = ReviewStatus.APPROVED
        self.reviewed_by = reviewer
        self.reviewed_at = datetime.now(UTC)
        self.review_note = _clean_optional_text(note)
        signal = self._record_signal(
            decision=HumanDecision.APPROVED,
            reviewer=reviewer,
            reason=note,
            requirement=requirement,
        )
        return CandidateApproval(requirement=requirement, learning_signal=signal)

    def reject(self, *, reviewer: Person, reason: str) -> LearningSignal:
        if reviewer is None:
            raise DomainValidationError("reviewer is required")
        self._require_pending_review("reject")
        rejection_reason = _require_text(reason, "reason")
        self.review_status = ReviewStatus.REJECTED
        self.reviewed_by = reviewer
        self.reviewed_at = datetime.now(UTC)
        self.review_note = rejection_reason
        return self._record_signal(
            decision=HumanDecision.REJECTED,
            reviewer=reviewer,
            reason=rejection_reason,
            requirement=None,
        )

    def _record_signal(
        self,
        *,
        decision: HumanDecision,
        reviewer: Person,
        reason: str | None,
        requirement: Requirement | None,
    ) -> LearningSignal:
        signal = LearningSignal(
            candidate_id=self.id,
            source_document=self.source_document_snapshot.model_copy(deep=True),
            source_location=self.source_location,
            original_discovery=self.original_discovery.model_copy(deep=True),
            reviewed_candidate=self._current_snapshot(),
            discovery_provenance=self.original_discovery.provenance,
            discovery_reason=self.original_discovery.discovery_reason,
            original_confidence=self.original_discovery.confidence,
            human_decision=decision,
            reviewer=PersonSnapshot.from_person(reviewer),
            decision_reason=_clean_optional_text(reason),
            resulting_requirement=(
                RequirementResultSnapshot.from_requirement(requirement)
                if requirement
                else None
            ),
            decided_at=self.reviewed_at or datetime.now(UTC),
        )
        self.learning_signals.append(signal)
        return signal

    def _current_snapshot(self) -> CandidateSnapshot:
        return CandidateSnapshot(
            title=self.title,
            description=self.description,
            due_date=self.due_date,
            target=self.target,
            owner=PersonSnapshot.from_person(self.owner) if self.owner else None,
            supervisor=(
                PersonSnapshot.from_person(self.supervisor)
                if self.supervisor
                else None
            ),
            evidence_expectation=self.evidence_expectation,
            provenance=self.provenance,
            classification=self.classification,
            source_document_id=self.source_document.id,
            source_location=self.source_location,
            discovery_reason=self.discovery_reason,
            confidence=self.confidence,
        )

    def _require_pending_review(self, action: str) -> None:
        if self.review_status != ReviewStatus.PENDING_REVIEW:
            raise InvalidTransitionError(
                f"cannot {action} a candidate in review status {self.review_status}"
            )


def _clean_optional_text(value: str | None) -> str | None:
    return value.strip() if value and value.strip() else None


def _is_present(value: object | None) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))
