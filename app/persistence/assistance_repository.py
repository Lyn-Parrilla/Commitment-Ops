"""Transactional storage for assistance proposals and lifecycle events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.domain.assistance import (
    AssistanceProposal,
    DetectedCondition,
    ProposalDecision,
    ProposalEvent,
    ProposalEventType,
    ProposalState,
    RecommendationType,
    RequirementFacts,
    measure_outcome,
)
from app.domain.enums import RequirementStatus
from app.domain.exceptions import InvalidTransitionError
from app.persistence.database import SessionFactory, raise_persistence_failure
from app.persistence.exceptions import PersistenceError
from app.persistence.orm import (
    AssistanceProposalEventRecord,
    AssistanceProposalRecord,
    RequirementRecord,
)
from app.persistence.rls import tenant_session

_OPEN_STATES = (ProposalState.PROPOSED.value, ProposalState.APPROVED.value)


@dataclass(frozen=True)
class StoredProposalEvent:
    id: UUID
    event_type: ProposalEventType
    actor_user_id: UUID
    occurred_at: datetime
    detail: dict[str, Any]


@dataclass(frozen=True)
class StoredProposal:
    id: UUID
    organization_id: UUID
    workflow_id: UUID
    requirement_id: UUID
    recommendation_type: RecommendationType
    rationale: str
    intended_outcome: str
    state: ProposalState
    created_at: datetime
    events: tuple[StoredProposalEvent, ...]


class AssistanceRepository:
    """Persist proposals. Lifecycle events are inserted and never rewritten."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def ensure_detected(
        self,
        conditions: list[DetectedCondition],
        *,
        organization_id: UUID,
        workflow_id: UUID,
        user_id: UUID,
        created_at: datetime,
    ) -> list[StoredProposal]:
        for attempt in range(2):
            try:
                return self._insert_detected(
                    conditions,
                    organization_id=organization_id,
                    workflow_id=workflow_id,
                    user_id=user_id,
                    created_at=created_at,
                )
            except IntegrityError as exc:
                if attempt == 1 or not _is_open_proposal_conflict(exc):
                    raise_persistence_failure(
                        "assistance proposal persistence failed",
                        exc,
                    )
            except PersistenceError:
                raise
            except SQLAlchemyError as exc:
                raise_persistence_failure(
                    "assistance proposal persistence failed",
                    exc,
                )
        raise PersistenceError("assistance proposal persistence failed")

    def _insert_detected(
        self,
        conditions: list[DetectedCondition],
        *,
        organization_id: UUID,
        workflow_id: UUID,
        user_id: UUID,
        created_at: datetime,
    ) -> list[StoredProposal]:
        with tenant_session(
            self._session_factory,
            write=True,
            user_id=user_id,
            organization_id=organization_id,
        ) as session:
            existing = {
                (record.requirement_id, record.recommendation_type): record
                for record in session.scalars(
                    select(AssistanceProposalRecord).where(
                        AssistanceProposalRecord.organization_id
                        == organization_id,
                        AssistanceProposalRecord.workflow_id == workflow_id,
                        AssistanceProposalRecord.lifecycle_state.in_(
                            _OPEN_STATES
                        ),
                    )
                )
            }
            ensured: list[AssistanceProposalRecord] = []
            for condition in conditions:
                key = (
                    condition.requirement_id,
                    condition.recommendation_type.value,
                )
                record = existing.get(key)
                if record is None:
                    record = AssistanceProposalRecord(
                        id=uuid4(),
                        organization_id=organization_id,
                        workflow_id=workflow_id,
                        requirement_id=condition.requirement_id,
                        recommendation_type=condition.recommendation_type.value,
                        rationale=condition.rationale,
                        intended_outcome=condition.intended_outcome,
                        lifecycle_state=ProposalState.PROPOSED.value,
                        created_at=created_at,
                    )
                    session.add(record)
                    session.flush()
                    session.add(
                        _event_record(
                            proposal_id=record.id,
                            organization_id=organization_id,
                            event=ProposalEvent(
                                event_type=ProposalEventType.PROPOSED,
                                actor_user_id=user_id,
                                occurred_at=created_at,
                                detail={
                                    "recommendation_type": (
                                        condition.recommendation_type.value
                                    ),
                                    "observed": condition.observed,
                                },
                            ),
                        )
                    )
                    existing[key] = record
                ensured.append(record)
            session.flush()
            loaded = [
                _load(session, record.id, organization_id, workflow_id)
                for record in ensured
            ]
        return [proposal for proposal in loaded if proposal is not None]

    def list_for_workflow(
        self,
        workflow_id: UUID,
        *,
        organization_id: UUID,
        user_id: UUID,
    ) -> list[StoredProposal]:
        try:
            with tenant_session(
                self._session_factory,
                write=False,
                user_id=user_id,
                organization_id=organization_id,
            ) as session:
                records = session.scalars(
                    select(AssistanceProposalRecord)
                    .where(
                        AssistanceProposalRecord.organization_id == organization_id,
                        AssistanceProposalRecord.workflow_id == workflow_id,
                    )
                    .order_by(
                        AssistanceProposalRecord.created_at,
                        AssistanceProposalRecord.id,
                    )
                ).all()
                return [
                    _to_stored(record, _events(session, record.id))
                    for record in records
                ]
        except SQLAlchemyError as exc:
            raise_persistence_failure("assistance proposal lookup failed", exc)

    def get(
        self,
        proposal_id: UUID,
        *,
        organization_id: UUID,
        workflow_id: UUID,
        user_id: UUID,
    ) -> StoredProposal | None:
        try:
            with tenant_session(
                self._session_factory,
                write=False,
                user_id=user_id,
                organization_id=organization_id,
            ) as session:
                loaded = _load(session, proposal_id, organization_id, workflow_id)
                return loaded
        except SQLAlchemyError as exc:
            raise_persistence_failure("assistance proposal lookup failed", exc)

    def decide(
        self,
        proposal_id: UUID,
        decision: ProposalDecision,
        *,
        organization_id: UUID,
        workflow_id: UUID,
        user_id: UUID,
        decided_at: datetime,
    ) -> StoredProposal | None:
        try:
            with tenant_session(
                self._session_factory,
                write=True,
                user_id=user_id,
                organization_id=organization_id,
            ) as session:
                proposal = _editable(session, proposal_id, organization_id, workflow_id)
                if proposal is None:
                    return None
                event = proposal.decide(
                    decision,
                    actor_user_id=user_id,
                    decided_at=decided_at,
                )
                _set_state(session, proposal)
                session.add(
                    _event_record(
                        proposal_id=proposal.id,
                        organization_id=organization_id,
                        event=event,
                    )
                )
                session.flush()
                return _load(session, proposal.id, organization_id, workflow_id)
        except InvalidTransitionError:
            raise
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise_persistence_failure("assistance proposal persistence failed", exc)

    def execute(
        self,
        proposal_id: UUID,
        *,
        organization_id: UUID,
        workflow_id: UUID,
        user_id: UUID,
        executed_at: datetime,
        today: date,
    ) -> StoredProposal | None:
        try:
            with tenant_session(
                self._session_factory,
                write=True,
                user_id=user_id,
                organization_id=organization_id,
            ) as session:
                proposal = _editable(session, proposal_id, organization_id, workflow_id)
                if proposal is None:
                    return None
                measurement = measure_outcome(
                    proposal.recommendation_type,
                    _requirement(session, proposal),
                    today=today,
                )
                executed, outcome = proposal.execute(
                    actor_user_id=user_id,
                    executed_at=executed_at,
                    measurement=measurement,
                )
                _set_state(session, proposal)
                for index, event in enumerate((executed, outcome)):
                    session.add(
                        _event_record(
                            proposal_id=proposal.id,
                            organization_id=organization_id,
                            event=event,
                            occurred_at=event.occurred_at
                            + timedelta(microseconds=index),
                        )
                    )
                session.flush()
                return _load(session, proposal.id, organization_id, workflow_id)
        except InvalidTransitionError:
            raise
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise_persistence_failure("assistance proposal persistence failed", exc)


def _is_open_proposal_conflict(exc: IntegrityError) -> bool:
    original = getattr(exc, "orig", None)
    constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
    if constraint == "uq_assistance_proposals_open_condition":
        return True
    message = str(original if original is not None else exc)
    return "uq_assistance_proposals_open_condition" in message or (
        "UNIQUE constraint failed" in message
        and "assistance_proposals.organization_id" in message
        and "assistance_proposals.recommendation_type" in message
    )


def _editable(
    session,
    proposal_id: UUID,
    organization_id: UUID,
    workflow_id: UUID,
) -> AssistanceProposal | None:
    record = session.get(AssistanceProposalRecord, proposal_id)
    if (
        record is None
        or record.organization_id != organization_id
        or record.workflow_id != workflow_id
    ):
        return None
    return AssistanceProposal(
        proposal_id=record.id,
        organization_id=record.organization_id,
        workflow_id=record.workflow_id,
        requirement_id=record.requirement_id,
        recommendation_type=RecommendationType(record.recommendation_type),
        rationale=record.rationale,
        intended_outcome=record.intended_outcome,
        state=ProposalState(record.lifecycle_state),
    )


def _set_state(session, proposal: AssistanceProposal) -> None:
    record = session.get(AssistanceProposalRecord, proposal.id)
    if record is None:
        raise PersistenceError("assistance proposal persistence failed")
    record.lifecycle_state = proposal.state.value


def _requirement(session, proposal: AssistanceProposal) -> RequirementFacts | None:
    record = session.get(RequirementRecord, proposal.requirement_id)
    if record is None or record.workflow_id != proposal.workflow_id:
        return None
    return RequirementFacts(
        requirement_id=record.id,
        status=RequirementStatus(record.status),
        due_date=record.due_date,
    )


def _load(session, proposal_id: UUID, organization_id: UUID, workflow_id: UUID):
    record = session.get(AssistanceProposalRecord, proposal_id)
    if (
        record is None
        or record.organization_id != organization_id
        or record.workflow_id != workflow_id
    ):
        return None
    return _to_stored(record, _events(session, record.id))


def _events(session, proposal_id: UUID) -> list[AssistanceProposalEventRecord]:
    return list(
        session.scalars(
            select(AssistanceProposalEventRecord)
            .where(AssistanceProposalEventRecord.proposal_id == proposal_id)
            .order_by(
                AssistanceProposalEventRecord.occurred_at,
                AssistanceProposalEventRecord.id,
            )
        )
    )


def _to_stored(
    record: AssistanceProposalRecord,
    events: list[AssistanceProposalEventRecord],
) -> StoredProposal:
    return StoredProposal(
        id=record.id,
        organization_id=record.organization_id,
        workflow_id=record.workflow_id,
        requirement_id=record.requirement_id,
        recommendation_type=RecommendationType(record.recommendation_type),
        rationale=record.rationale,
        intended_outcome=record.intended_outcome,
        state=ProposalState(record.lifecycle_state),
        created_at=record.created_at,
        events=tuple(
            StoredProposalEvent(
                id=event.id,
                event_type=ProposalEventType(event.event_type),
                actor_user_id=event.actor_user_id,
                occurred_at=event.occurred_at,
                detail=event.detail,
            )
            for event in events
        ),
    )


def _event_record(
    *,
    proposal_id: UUID,
    organization_id: UUID,
    event: ProposalEvent,
    occurred_at: datetime | None = None,
) -> AssistanceProposalEventRecord:
    return AssistanceProposalEventRecord(
        id=uuid4(),
        organization_id=organization_id,
        proposal_id=proposal_id,
        event_type=event.event_type.value,
        actor_user_id=event.actor_user_id,
        occurred_at=event.occurred_at if occurred_at is None else occurred_at,
        detail=event.detail,
    )
