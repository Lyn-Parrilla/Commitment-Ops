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
    Index,
    String,
    Text,
    TypeDecorator,
    Uuid,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
