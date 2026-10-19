"""Relational records for the persisted accountability aggregate."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Float,
    Index,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    Uuid,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.assistance import (
    ProposalEventType,
    ProposalState,
    RecommendationType,
)
from app.domain.discovery import (
    DiscoveryClassification,
    HumanDecision,
)
from app.domain.enums import (
    EvidenceStatus,
    EvidenceType,
    RequirementProvenance,
    RequirementStatus,
    ReviewStatus,
    WorkflowStatus,
    WorkflowType,
)
from app.persistence.database import Base


def _allowed_values(enum_type: type) -> str:
    return ", ".join(f"'{item.value}'" for item in enum_type)


class UTCDateTime(TypeDecorator[datetime]):
    """Store UTC and restore timezone information consistently on SQLite."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: object
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(UTC)

    def process_result_value(
        self, value: datetime | None, dialect: object
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class PersonRecord(Base):
    __tablename__ = "people"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    role: Mapped[str | None] = mapped_column(String(200))


class OrganizationRecord(Base):
    __tablename__ = "organizations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(300))
    logo_url: Mapped[str | None] = mapped_column(String(2000))
    primary_color: Mapped[str | None] = mapped_column(String(7))
    accent_color: Mapped[str | None] = mapped_column(String(7))
    favicon_url: Mapped[str | None] = mapped_column(String(2000))


class OrganizationMembershipRecord(Base):
    __tablename__ = "organization_memberships"
    __table_args__ = (
        Index(
            "ix_organization_memberships_user_active",
            "user_id",
            "is_active",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )


class WorkflowRecord(Base):
    __tablename__ = "workflows"
    __table_args__ = (
        CheckConstraint(
            f"workflow_type IN ({_allowed_values(WorkflowType)})",
            name="ck_workflows_type",
        ),
        CheckConstraint(
            f"status IN ({_allowed_values(WorkflowStatus)})",
            name="ck_workflows_status",
        ),
        Index("ix_workflows_type_status", "workflow_type", "status"),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_workflows_id_organization_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    workflow_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)

    requirements: Mapped[list[RequirementRecord]] = relationship(
        back_populates="workflow",
        order_by="RequirementRecord.id",
    )


class RequirementRecord(Base):
    __tablename__ = "requirements"
    __table_args__ = (
        CheckConstraint(
            f"provenance IN ({_allowed_values(RequirementProvenance)})",
            name="ck_requirements_provenance",
        ),
        CheckConstraint(
            f"status IN ({_allowed_values(RequirementStatus)})",
            name="ck_requirements_status",
        ),
        CheckConstraint(
            f"review_status IN ({_allowed_values(ReviewStatus)})",
            name="ck_requirements_review_status",
        ),
        Index("ix_requirements_workflow_status", "workflow_id", "status"),
        Index("ix_requirements_due_date", "due_date"),
        UniqueConstraint(
            "id",
            "workflow_id",
            name="uq_requirements_id_workflow_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workflow_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("workflows.id", ondelete="RESTRICT"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "people.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=False,
    )
    supervisor_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "people.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    evidence_expectation: Mapped[str] = mapped_column(Text, nullable=False)
    provenance: Mapped[str] = mapped_column(String(40), nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date)
    target: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    review_status: Mapped[str] = mapped_column(String(30), nullable=False)
    reviewed_by_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "people.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    review_note: Mapped[str | None] = mapped_column(Text)

    workflow: Mapped[WorkflowRecord] = relationship(back_populates="requirements")
    owner: Mapped[PersonRecord] = relationship(foreign_keys=[owner_id])
    supervisor: Mapped[PersonRecord | None] = relationship(
        foreign_keys=[supervisor_id]
    )
    reviewed_by: Mapped[PersonRecord | None] = relationship(
        foreign_keys=[reviewed_by_id]
    )
    evidence: Mapped[list[EvidenceRecord]] = relationship(
        back_populates="requirement",
        order_by="EvidenceRecord.id",
    )


class EvidenceRecord(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        CheckConstraint(
            f"evidence_type IN ({_allowed_values(EvidenceType)})",
            name="ck_evidence_type",
        ),
        CheckConstraint(
            f"status IN ({_allowed_values(EvidenceStatus)})",
            name="ck_evidence_status",
        ),
        Index("ix_evidence_requirement", "requirement_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    requirement_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("requirements.id", ondelete="RESTRICT"),
        nullable=False,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(30), nullable=False)
    submitted_by_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "people.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=False,
    )
    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(30), nullable=False)

    requirement: Mapped[RequirementRecord] = relationship(
        back_populates="evidence"
    )
    submitted_by: Mapped[PersonRecord] = relationship(
        foreign_keys=[submitted_by_id]
    )


class AuditEventRecord(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "length(actor_reference) <= 255",
            name="ck_audit_actor_reference_length",
        ),
        CheckConstraint(
            "length(correlation_id) <= 255",
            name="ck_audit_correlation_id_length",
        ),
        Index(
            "ix_audit_entity_history",
            "entity_type",
            "entity_id",
            "occurred_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    before_state: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after_state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    actor_reference: Mapped[str | None] = mapped_column(String(255))
    correlation_id: Mapped[str | None] = mapped_column(String(255))


class RequirementCandidateRecord(Base):
    __tablename__ = "requirement_candidates"
    __table_args__ = (
        CheckConstraint(
            f"provenance IN ({_allowed_values(RequirementProvenance)})",
            name="ck_requirement_candidates_provenance",
        ),
        CheckConstraint(
            f"classification IN ({_allowed_values(DiscoveryClassification)})",
            name="ck_requirement_candidates_classification",
        ),
        CheckConstraint(
            f"review_status IN ({_allowed_values(ReviewStatus)})",
            name="ck_requirement_candidates_review_status",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_requirement_candidates_id_organization_id",
        ),
        Index("ix_requirement_candidates_organization_id", "organization_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "organizations.id",
            name="fk_requirement_candidates_organization_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    provenance: Mapped[str] = mapped_column(String(40), nullable=False)
    classification: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    due_date: Mapped[date | None] = mapped_column(Date)
    target: Mapped[str | None] = mapped_column(String(500))
    evidence_expectation: Mapped[str | None] = mapped_column(Text)
    owner_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    supervisor_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    discovery_reason: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    source_document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_location: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    original_discovery: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    review_status: Mapped[str] = mapped_column(String(30), nullable=False)
    reviewed_by_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    review_note: Mapped[str | None] = mapped_column(Text)


class CandidateReviewDecisionRecord(Base):
    __tablename__ = "candidate_review_decisions"
    __table_args__ = (
        CheckConstraint(
            f"human_decision IN ({_allowed_values(HumanDecision)})",
            name="ck_candidate_review_decisions_human_decision",
        ),
        ForeignKeyConstraint(
            ["candidate_id", "organization_id"],
            ["requirement_candidates.id", "requirement_candidates.organization_id"],
            name="fk_candidate_review_decisions_candidate_organization",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_candidate_review_decisions_id_organization_id",
        ),
        Index(
            "ix_candidate_review_decisions_organization_id",
            "organization_id",
        ),
        Index("ix_candidate_review_decisions_candidate_id", "candidate_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "organizations.id",
            name="fk_candidate_review_decisions_organization_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    candidate_id: Mapped[UUID | None] = mapped_column(Uuid)
    human_decision: Mapped[str] = mapped_column(String(20), nullable=False)
    reviewer_user_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    reviewer_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class LearningSignalRecord(Base):
    __tablename__ = "learning_signals"
    __table_args__ = (
        CheckConstraint(
            "discovery_provenance IS NULL OR discovery_provenance IN "
            f"({_allowed_values(RequirementProvenance)})",
            name="ck_learning_signals_discovery_provenance",
        ),
        ForeignKeyConstraint(
            ["decision_id", "organization_id"],
            [
                "candidate_review_decisions.id",
                "candidate_review_decisions.organization_id",
            ],
            name="fk_learning_signals_decision_organization",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["candidate_id", "organization_id"],
            [
                "requirement_candidates.id",
                "requirement_candidates.organization_id",
            ],
            name="fk_learning_signals_candidate_organization",
            ondelete="RESTRICT",
        ),
        Index("ix_learning_signals_organization_id", "organization_id"),
        Index("ix_learning_signals_candidate_id", "candidate_id"),
        UniqueConstraint("decision_id", name="uq_learning_signals_decision_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "organizations.id",
            name="fk_learning_signals_organization_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    decision_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    candidate_id: Mapped[UUID | None] = mapped_column(Uuid)
    source_document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_location: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    original_discovery: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reviewed_candidate: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    discovery_provenance: Mapped[str | None] = mapped_column(String(40))
    discovery_reason: Mapped[str | None] = mapped_column(Text)
    original_confidence: Mapped[float | None] = mapped_column(Float)
    resulting_requirement: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class AssistanceProposalRecord(Base):
    __tablename__ = "assistance_proposals"
    __table_args__ = (
        CheckConstraint(
            f"recommendation_type IN ({_allowed_values(RecommendationType)})",
            name="ck_assistance_proposals_recommendation_type",
        ),
        CheckConstraint(
            f"lifecycle_state IN ({_allowed_values(ProposalState)})",
            name="ck_assistance_proposals_lifecycle_state",
        ),
        ForeignKeyConstraint(
            ["workflow_id", "organization_id"],
            ["workflows.id", "workflows.organization_id"],
            name="fk_assistance_proposals_workflow_organization",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["requirement_id", "workflow_id"],
            ["requirements.id", "requirements.workflow_id"],
            name="fk_assistance_proposals_requirement_workflow",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_assistance_proposals_id_organization_id",
        ),
        Index("ix_assistance_proposals_organization_id", "organization_id"),
        Index("ix_assistance_proposals_workflow_id", "workflow_id"),
        Index(
            "uq_assistance_proposals_open_condition",
            "organization_id",
            "workflow_id",
            "requirement_id",
            "recommendation_type",
            unique=True,
            postgresql_where=text(
                "lifecycle_state IN ('proposed', 'approved')"
            ),
            sqlite_where=text(
                "lifecycle_state IN ('proposed', 'approved')"
            ),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "organizations.id",
            name="fk_assistance_proposals_organization_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    workflow_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    requirement_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    recommendation_type: Mapped[str] = mapped_column(String(40), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    intended_outcome: Mapped[str] = mapped_column(Text, nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AssistanceProposalEventRecord(Base):
    __tablename__ = "assistance_proposal_events"
    __table_args__ = (
        CheckConstraint(
            f"event_type IN ({_allowed_values(ProposalEventType)})",
            name="ck_assistance_proposal_events_event_type",
        ),
        ForeignKeyConstraint(
            ["proposal_id", "organization_id"],
            [
                "assistance_proposals.id",
                "assistance_proposals.organization_id",
            ],
            name="fk_assistance_proposal_events_proposal_organization",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "proposal_id",
            "event_type",
            name="uq_assistance_proposal_events_proposal_type",
        ),
        Index(
            "ix_assistance_proposal_events_organization_id",
            "organization_id",
        ),
        Index("ix_assistance_proposal_events_proposal_id", "proposal_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "organizations.id",
            name="fk_assistance_proposal_events_organization_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    proposal_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_user_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
