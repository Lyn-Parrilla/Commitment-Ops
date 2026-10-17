"""Transactional repository for the authoritative workflow aggregate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.domain.enums import (
    EvidenceStatus,
    EvidenceType,
    RequirementProvenance,
    RequirementStatus,
    ReviewStatus,
    WorkflowStatus,
    WorkflowType,
)
from app.domain.models import Evidence, Person, Requirement, Workflow
from app.persistence.database import SessionFactory, raise_persistence_failure
from app.persistence.exceptions import InvalidAssociationError, PersistenceError
from app.persistence.orm import (
    AuditEventRecord,
    EvidenceRecord,
    OrganizationMembershipRecord,
    OrganizationRecord,
    PersonRecord,
    RequirementRecord,
    WorkflowRecord,
)


@dataclass(frozen=True)
class AuditEvent:
    id: UUID
    occurred_at: datetime
    entity_type: str
    entity_id: UUID
    event_type: str
    before_state: dict[str, Any] | None
    after_state: dict[str, Any]
    actor_reference: str | None
    correlation_id: str | None


@dataclass(frozen=True)
class Organization:
    id: UUID
    name: str
    display_name: str | None = None
    logo_url: str | None = None
    primary_color: str | None = None
    accent_color: str | None = None
    favicon_url: str | None = None


class OrganizationRepository:
    """Persist organizations and resolve active user memberships."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def save(self, organization: Organization) -> None:
        try:
            with self._session_factory.begin() as session:
                record = session.get(OrganizationRecord, organization.id)
                if record is None:
                    session.add(_organization_record(organization))
                else:
                    _apply_organization(record, organization)
        except SQLAlchemyError as exc:
            raise_persistence_failure("organization persistence failed", exc)

    def set_membership(
        self,
        *,
        organization_id: UUID,
        user_id: UUID,
        is_active: bool,
    ) -> None:
        try:
            with self._session_factory.begin() as session:
                record = session.get(
                    OrganizationMembershipRecord,
                    (organization_id, user_id),
                )
                if record is None:
                    session.add(
                        OrganizationMembershipRecord(
                            organization_id=organization_id,
                            user_id=user_id,
                            is_active=is_active,
                        )
                    )
                else:
                    record.is_active = is_active
        except SQLAlchemyError as exc:
            raise_persistence_failure("membership persistence failed", exc)

    def has_active_membership(
        self,
        *,
        organization_id: UUID,
        user_id: UUID,
    ) -> bool:
        try:
            with self._session_factory() as session:
                return (
                    session.scalar(
                        select(OrganizationMembershipRecord).where(
                            OrganizationMembershipRecord.organization_id
                            == organization_id,
                            OrganizationMembershipRecord.user_id == user_id,
                            OrganizationMembershipRecord.is_active.is_(True),
                        )
                    )
                    is not None
                )
        except SQLAlchemyError as exc:
            raise_persistence_failure("membership lookup failed", exc)

    def list_active_for_user(self, user_id: UUID) -> list[Organization]:
        """Return organizations where this user has an active membership."""

        try:
            with self._session_factory() as session:
                records = session.scalars(
                    select(OrganizationRecord)
                    .join(
                        OrganizationMembershipRecord,
                        OrganizationMembershipRecord.organization_id
                        == OrganizationRecord.id,
                    )
                    .where(
                        OrganizationMembershipRecord.user_id == user_id,
                        OrganizationMembershipRecord.is_active.is_(True),
                    )
                    .order_by(OrganizationRecord.name, OrganizationRecord.id)
                ).all()
                return [_to_organization(record) for record in records]
        except SQLAlchemyError as exc:
            raise_persistence_failure("organization lookup failed", exc)


def _organization_record(organization: Organization) -> OrganizationRecord:
    record = OrganizationRecord(id=organization.id, name=organization.name)
    _apply_organization(record, organization)
    return record


def _apply_organization(
    record: OrganizationRecord,
    organization: Organization,
) -> None:
    record.name = organization.name
    record.display_name = organization.display_name
    record.logo_url = organization.logo_url
    record.primary_color = organization.primary_color
    record.accent_color = organization.accent_color
    record.favicon_url = organization.favicon_url


def _to_organization(record: OrganizationRecord) -> Organization:
    return Organization(
        id=record.id,
        name=record.name,
        display_name=record.display_name,
        logo_url=record.logo_url,
        primary_color=record.primary_color,
        accent_color=record.accent_color,
        favicon_url=record.favicon_url,
    )


class WorkflowRepository:
    """Persist a workflow and its accountability records as one transaction."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def save(
        self,
        workflow: Workflow,
        *,
        actor_reference: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        if workflow.organization_id is None:
            raise InvalidAssociationError(
                "workflow must belong to an organization"
            )
        self._validate_aggregate(workflow)
        try:
            with self._session_factory.begin() as session:
                existing = session.get(WorkflowRecord, workflow.id)
                before_state = (
                    _audit_snapshot(_to_domain(existing)) if existing else None
                )
                self._save_aggregate(session, workflow, existing)
                session.add(
                    AuditEventRecord(
                        occurred_at=datetime.now(UTC),
                        entity_type="workflow",
                        entity_id=workflow.id,
                        event_type=(
                            "workflow.updated" if existing else "workflow.created"
                        ),
                        before_state=before_state,
                        after_state=_audit_snapshot(workflow),
                        actor_reference=_clean_optional(actor_reference),
                        correlation_id=_clean_optional(correlation_id),
                    )
                )
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise_persistence_failure("workflow persistence failed", exc)

    def get(
        self,
        workflow_id: UUID,
        *,
        organization_id: UUID,
    ) -> Workflow | None:
        try:
            with self._session_factory() as session:
                record = session.scalar(
                    _workflow_select().where(
                        WorkflowRecord.id == workflow_id,
                        WorkflowRecord.organization_id == organization_id,
                    )
                )
                return _to_domain(record) if record else None
        except SQLAlchemyError as exc:
            raise_persistence_failure("workflow lookup failed", exc)

    def list(
        self,
        *,
        organization_id: UUID,
        status: WorkflowStatus | None = None,
        workflow_type: WorkflowType | None = None,
    ) -> list[Workflow]:
        statement = _workflow_select().where(
            WorkflowRecord.organization_id == organization_id
        )
        if status is not None:
            statement = statement.where(WorkflowRecord.status == status.value)
        if workflow_type is not None:
            statement = statement.where(
                WorkflowRecord.workflow_type == workflow_type.value
            )
        statement = statement.order_by(WorkflowRecord.name, WorkflowRecord.id)
        try:
            with self._session_factory() as session:
                return [
                    _to_domain(record)
                    for record in session.scalars(statement).unique().all()
                ]
        except SQLAlchemyError as exc:
            raise_persistence_failure("workflow lookup failed", exc)

    def list_audit_events(
        self,
        workflow_id: UUID,
        *,
        organization_id: UUID,
    ) -> list[AuditEvent]:
        statement = (
            select(AuditEventRecord)
            .join(
                WorkflowRecord,
                WorkflowRecord.id == AuditEventRecord.entity_id,
            )
            .where(
                AuditEventRecord.entity_type == "workflow",
                AuditEventRecord.entity_id == workflow_id,
                WorkflowRecord.organization_id == organization_id,
            )
            .order_by(AuditEventRecord.occurred_at, AuditEventRecord.id)
        )
        try:
            with self._session_factory() as session:
                return [
                    AuditEvent(
                        id=record.id,
                        occurred_at=record.occurred_at,
                        entity_type=record.entity_type,
                        entity_id=record.entity_id,
                        event_type=record.event_type,
                        before_state=record.before_state,
                        after_state=record.after_state,
                        actor_reference=record.actor_reference,
                        correlation_id=record.correlation_id,
                    )
                    for record in session.scalars(statement)
                ]
        except SQLAlchemyError as exc:
            raise_persistence_failure("audit lookup failed", exc)

    def _save_aggregate(
        self,
        session: Session,
        workflow: Workflow,
        existing: WorkflowRecord | None,
    ) -> None:
        people = _collect_people(workflow)
        for person in people.values():
            record = session.get(PersonRecord, person.id)
            if record is None:
                session.add(
                    PersonRecord(
                        id=person.id,
                        name=person.name,
                        email=person.email,
                        role=person.role,
                    )
                )
            elif (
                record.name != person.name
                or record.email != person.email
                or record.role != person.role
            ):
                raise InvalidAssociationError(
                    f"person {person.id} conflicts with persisted identity data"
                )

        if existing is None:
            existing = WorkflowRecord(
                id=workflow.id,
                organization_id=workflow.organization_id,
            )
            session.add(existing)
        elif existing.organization_id != workflow.organization_id:
            raise InvalidAssociationError(
                "workflow cannot move between organizations"
            )
        existing.name = workflow.name
        existing.workflow_type = workflow.workflow_type.value
        existing.status = workflow.status.value

        incoming_requirement_ids = {
            requirement.id for requirement in workflow.requirements
        }
        current_requirement_ids = {
            requirement.id for requirement in existing.requirements
        }
        removed_requirements = current_requirement_ids - incoming_requirement_ids
        if removed_requirements:
            raise InvalidAssociationError(
                "save cannot remove requirements from a persisted workflow"
            )

        persisted_requirements = _records_by_id(
            session,
            RequirementRecord,
            incoming_requirement_ids,
        )
        for record in persisted_requirements.values():
            if record.workflow_id != workflow.id:
                raise InvalidAssociationError(
                    f"requirement {record.id} belongs to another workflow"
                )

        for requirement in workflow.requirements:
            record = persisted_requirements.get(requirement.id)
            if record is None:
                record = RequirementRecord(
                    id=requirement.id,
                    workflow_id=workflow.id,
                )
                session.add(record)
            _copy_requirement(requirement, record)
            self._save_evidence(session, requirement, record)

    def _save_evidence(
        self,
        session: Session,
        requirement: Requirement,
        requirement_record: RequirementRecord,
    ) -> None:
        incoming_ids = {evidence.id for evidence in requirement.evidence}
        current_ids = {evidence.id for evidence in requirement_record.evidence}
        if current_ids - incoming_ids:
            raise InvalidAssociationError(
                "save cannot remove evidence from a persisted requirement"
            )

        persisted = _records_by_id(session, EvidenceRecord, incoming_ids)
        for record in persisted.values():
            if record.requirement_id != requirement.id:
                raise InvalidAssociationError(
                    f"evidence {record.id} belongs to another requirement"
                )

        for evidence in requirement.evidence:
            record = persisted.get(evidence.id)
            if record is None:
                record = EvidenceRecord(
                    id=evidence.id,
                    requirement_id=requirement.id,
                )
                session.add(record)
            record.description = evidence.description
            record.evidence_type = evidence.evidence_type.value
            record.submitted_by_id = evidence.submitted_by.id
            record.submitted_at = evidence.submitted_at
            record.reference = evidence.reference
            record.status = evidence.status.value

    def _validate_aggregate(self, workflow: Workflow) -> None:
        requirement_ids: set[UUID] = set()
        evidence_ids: set[UUID] = set()
        people: dict[UUID, Person] = {}

        for requirement in workflow.requirements:
            if requirement.id in requirement_ids:
                raise InvalidAssociationError(
                    f"duplicate requirement association: {requirement.id}"
                )
            requirement_ids.add(requirement.id)
            if (
                requirement.provenance == RequirementProvenance.AI_INFERRED
                and requirement.review_status == ReviewStatus.APPROVED
                and (
                    requirement.reviewed_by is None
                    or requirement.reviewed_at is None
                )
            ):
                raise InvalidAssociationError(
                    "approved AI-inferred requirements require human review metadata"
                )

            for person in (
                requirement.owner,
                requirement.supervisor,
                requirement.reviewed_by,
            ):
                _remember_person(people, person)

            for evidence in requirement.evidence:
                if evidence.id in evidence_ids:
                    raise InvalidAssociationError(
                        f"duplicate evidence association: {evidence.id}"
                    )
                evidence_ids.add(evidence.id)
                _remember_person(people, evidence.submitted_by)


def _workflow_select() -> Select[tuple[WorkflowRecord]]:
    return select(WorkflowRecord).options(
        selectinload(WorkflowRecord.requirements).selectinload(
            RequirementRecord.owner
        ),
        selectinload(WorkflowRecord.requirements).selectinload(
            RequirementRecord.supervisor
        ),
        selectinload(WorkflowRecord.requirements).selectinload(
            RequirementRecord.reviewed_by
        ),
        selectinload(WorkflowRecord.requirements)
        .selectinload(RequirementRecord.evidence)
        .selectinload(EvidenceRecord.submitted_by),
    )


def _records_by_id(
    session: Session,
    record_type: type[RequirementRecord] | type[EvidenceRecord],
    identifiers: set[UUID],
) -> dict[UUID, RequirementRecord | EvidenceRecord]:
    if not identifiers:
        return {}
    records = session.scalars(
        select(record_type).where(record_type.id.in_(identifiers))
    ).all()
    return {record.id: record for record in records}


def _collect_people(workflow: Workflow) -> dict[UUID, Person]:
    people: dict[UUID, Person] = {}
    for requirement in workflow.requirements:
        for person in (
            requirement.owner,
            requirement.supervisor,
            requirement.reviewed_by,
        ):
            _remember_person(people, person)
        for evidence in requirement.evidence:
            _remember_person(people, evidence.submitted_by)
    return people


def _remember_person(
    people: dict[UUID, Person], person: Person | None
) -> None:
    if person is None:
        return
    existing = people.get(person.id)
    if existing is not None and existing != person:
        raise InvalidAssociationError(
            f"person {person.id} has conflicting representations"
        )
    people[person.id] = person


def _copy_requirement(
    source: Requirement, target: RequirementRecord
) -> None:
    target.title = source.title
    target.description = source.description
    target.owner_id = source.owner.id
    target.supervisor_id = source.supervisor.id if source.supervisor else None
    target.evidence_expectation = source.evidence_expectation
    target.provenance = source.provenance.value
    target.due_date = source.due_date
    target.target = source.target
    target.status = source.status.value
    target.review_status = source.review_status.value
    target.reviewed_by_id = (
        source.reviewed_by.id if source.reviewed_by else None
    )
    target.reviewed_at = source.reviewed_at
    target.review_note = source.review_note


def _to_domain(record: WorkflowRecord) -> Workflow:
    people: dict[UUID, Person] = {}

    def person(person_record: PersonRecord | None) -> Person | None:
        if person_record is None:
            return None
        if person_record.id not in people:
            people[person_record.id] = Person(
                id=person_record.id,
                name=person_record.name,
                email=person_record.email,
                role=person_record.role,
            )
        return people[person_record.id]

    requirements: list[Requirement] = []
    for requirement_record in record.requirements:
        owner = person(requirement_record.owner)
        if owner is None:
            raise PersistenceError("persisted requirement has no owner")
        reviewed_by = person(requirement_record.reviewed_by)
        provenance = RequirementProvenance(requirement_record.provenance)
        review_status = ReviewStatus(requirement_record.review_status)
        if (
            provenance == RequirementProvenance.AI_INFERRED
            and review_status == ReviewStatus.APPROVED
            and (
                reviewed_by is None
                or requirement_record.reviewed_at is None
            )
        ):
            raise PersistenceError(
                "persisted AI-inferred approval lacks human review metadata"
            )

        evidence = [
            Evidence(
                id=evidence_record.id,
                description=evidence_record.description,
                evidence_type=EvidenceType(evidence_record.evidence_type),
                submitted_by=person(evidence_record.submitted_by),
                submitted_at=evidence_record.submitted_at,
                reference=evidence_record.reference,
                status=EvidenceStatus(evidence_record.status),
            )
            for evidence_record in requirement_record.evidence
        ]
        requirements.append(
            Requirement(
                id=requirement_record.id,
                title=requirement_record.title,
                description=requirement_record.description,
                owner=owner,
                supervisor=person(requirement_record.supervisor),
                evidence_expectation=requirement_record.evidence_expectation,
                provenance=provenance,
                due_date=requirement_record.due_date,
                target=requirement_record.target,
                status=RequirementStatus(requirement_record.status),
                review_status=review_status,
                reviewed_by=reviewed_by,
                reviewed_at=requirement_record.reviewed_at,
                review_note=requirement_record.review_note,
                evidence=evidence,
            )
        )

    return Workflow(
        id=record.id,
        name=record.name,
        workflow_type=WorkflowType(record.workflow_type),
        organization_id=record.organization_id,
        status=WorkflowStatus(record.status),
        requirements=requirements,
    )


def _audit_snapshot(workflow: Workflow) -> dict[str, Any]:
    return {
        "id": str(workflow.id),
        "organization_id": str(workflow.organization_id),
        "workflow_type": workflow.workflow_type.value,
        "status": workflow.status.value,
        "requirements": [
            {
                "id": str(requirement.id),
                "owner_id": str(requirement.owner.id),
                "supervisor_id": (
                    str(requirement.supervisor.id)
                    if requirement.supervisor
                    else None
                ),
                "due_date": (
                    requirement.due_date.isoformat()
                    if requirement.due_date
                    else None
                ),
                "status": requirement.status.value,
                "review_status": requirement.review_status.value,
                "provenance": requirement.provenance.value,
                "evidence": [
                    {
                        "id": str(evidence.id),
                        "status": evidence.status.value,
                        "submitted_by_id": str(evidence.submitted_by.id),
                    }
                    for evidence in requirement.evidence
                ],
            }
            for requirement in workflow.requirements
        ],
    }


def _clean_optional(value: str | None) -> str | None:
    return value.strip() if value and value.strip() else None
