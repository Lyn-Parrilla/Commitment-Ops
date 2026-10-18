"""Durable requirement-review decisions and separate learning signals."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.domain.discovery import (
    CandidateSnapshot,
    HumanDecision,
    LearningSignal,
    PersonSnapshot,
    RequirementCandidate,
    RequirementResultSnapshot,
    SourceDocument,
    SourceDocumentSnapshot,
    SourceLocation,
)
from app.domain.enums import RequirementProvenance, ReviewStatus
from app.domain.models import Person
from app.persistence.database import SessionFactory, raise_persistence_failure
from app.persistence.exceptions import PersistenceError
from app.persistence.orm import (
    CandidateReviewDecisionRecord,
    LearningSignalRecord,
    RequirementCandidateRecord,
)
from app.persistence.rls import tenant_session

_PROVENANCE_OVERWRITE = "refusing to overwrite recorded discovery provenance"
_DECISION_OVERWRITE = "refusing to overwrite a recorded review decision"
_SIGNAL_OVERWRITE = "refusing to overwrite a recorded learning signal"
_SIGNAL_DROP = "refusing to drop a recorded learning signal"


class DiscoveryRepository:
    """Persist candidates, immutable decisions, and immutable feedback."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def save_candidate(
        self,
        candidate: RequirementCandidate,
        *,
        organization_id: UUID,
        user_id: UUID,
    ) -> None:
        try:
            with tenant_session(
                self._session_factory,
                write=True,
                user_id=user_id,
                organization_id=organization_id,
            ) as session:
                _save_candidate(session, candidate, organization_id)
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise_persistence_failure("review persistence failed", exc)

    def get_candidate(
        self,
        candidate_id: UUID,
        *,
        organization_id: UUID,
        user_id: UUID,
    ) -> RequirementCandidate | None:
        try:
            with tenant_session(
                self._session_factory,
                write=False,
                user_id=user_id,
                organization_id=organization_id,
            ) as session:
                return _load_candidate(session, candidate_id, organization_id)
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise_persistence_failure("review lookup failed", exc)

    def save_human_added(
        self,
        signal: LearningSignal,
        *,
        organization_id: UUID,
        user_id: UUID,
    ) -> None:
        if signal.candidate_id is not None:
            raise PersistenceError(
                "human-added feedback is not attached to a candidate"
            )
        try:
            with tenant_session(
                self._session_factory,
                write=True,
                user_id=user_id,
                organization_id=organization_id,
            ) as session:
                _save_signal(session, signal, organization_id)
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise_persistence_failure("review persistence failed", exc)

    def get_signal(
        self,
        signal_id: UUID,
        *,
        organization_id: UUID,
        user_id: UUID,
    ) -> LearningSignal | None:
        try:
            with tenant_session(
                self._session_factory,
                write=False,
                user_id=user_id,
                organization_id=organization_id,
            ) as session:
                return _load_signal(session, signal_id, organization_id)
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise_persistence_failure("review lookup failed", exc)


def _save_candidate(
    session: Session,
    candidate: RequirementCandidate,
    organization_id: UUID,
) -> None:
    _require_feedback_for_terminal_review(candidate)
    record = session.get(RequirementCandidateRecord, candidate.id)
    if record is None:
        session.add(_candidate_record(candidate, organization_id))
    else:
        if record.organization_id != organization_id:
            raise PersistenceError(_PROVENANCE_OVERWRITE)
        _refuse_provenance_changes(record, candidate)
        _apply_candidate_edits(record, candidate)
    stored_signal_ids = set(
        session.scalars(
            select(LearningSignalRecord.id).where(
                LearningSignalRecord.candidate_id == candidate.id,
                LearningSignalRecord.organization_id == organization_id,
            )
        )
    )
    incoming_ids = {signal.id for signal in candidate.learning_signals}
    if not stored_signal_ids <= incoming_ids:
        raise PersistenceError(_SIGNAL_DROP)
    for signal in candidate.learning_signals:
        if signal.candidate_id != candidate.id:
            raise PersistenceError(
                "learning signal is not associated with this candidate"
            )
        _save_signal(session, signal, organization_id)


def _save_signal(
    session: Session,
    signal: LearningSignal,
    organization_id: UUID,
) -> None:
    if signal.decision_id == signal.id:
        raise PersistenceError(
            "review decision and learning signal must be distinct records"
        )
    decision = session.get(CandidateReviewDecisionRecord, signal.decision_id)
    payload = _decision_record(signal, organization_id)
    if decision is None:
        session.add(payload)
        session.flush()
    else:
        _refuse_decision_changes(decision, payload)
    existing = session.get(LearningSignalRecord, signal.id)
    signal_payload = _signal_record(signal, organization_id)
    if existing is None:
        session.add(signal_payload)
        return
    _refuse_signal_changes(existing, signal_payload)


def _load_candidate(
    session: Session,
    candidate_id: UUID,
    organization_id: UUID,
) -> RequirementCandidate | None:
    record = session.get(RequirementCandidateRecord, candidate_id)
    if record is None or record.organization_id != organization_id:
        return None
    signals = [
        _to_signal(session, signal_id, organization_id)
        for signal_id in session.scalars(
            select(LearningSignalRecord.id)
            .join(
                CandidateReviewDecisionRecord,
                CandidateReviewDecisionRecord.id == LearningSignalRecord.decision_id,
            )
            .where(
                LearningSignalRecord.candidate_id == candidate_id,
                LearningSignalRecord.organization_id == organization_id,
            )
            .order_by(
                CandidateReviewDecisionRecord.decided_at,
                LearningSignalRecord.id,
            )
        )
    ]
    return _to_candidate(record, signals)


def _load_signal(
    session: Session,
    signal_id: UUID,
    organization_id: UUID,
) -> LearningSignal | None:
    record = session.get(LearningSignalRecord, signal_id)
    if record is None or record.organization_id != organization_id:
        return None
    return _to_signal(session, signal_id, organization_id)


def _to_signal(
    session: Session,
    signal_id: UUID,
    organization_id: UUID,
) -> LearningSignal:
    record = session.get(LearningSignalRecord, signal_id)
    if record is None or record.organization_id != organization_id:
        raise PersistenceError("learning signal was not found")
    decision = session.get(CandidateReviewDecisionRecord, record.decision_id)
    if (
        decision is None
        or decision.organization_id != organization_id
        or decision.candidate_id != record.candidate_id
    ):
        raise PersistenceError("learning signal is missing its review decision")
    reviewer = PersonSnapshot.model_validate(decision.reviewer_snapshot)
    if reviewer.id != decision.reviewer_user_id:
        raise PersistenceError("reviewer identity does not match the decision")
    provenance = (
        None
        if record.discovery_provenance is None
        else RequirementProvenance(record.discovery_provenance)
    )
    return LearningSignal(
        id=record.id,
        decision_id=decision.id,
        candidate_id=record.candidate_id,
        source_document=SourceDocumentSnapshot.model_validate(record.source_document),
        source_location=_location(record.source_location),
        original_discovery=_candidate_snapshot(record.original_discovery),
        reviewed_candidate=_candidate_snapshot(record.reviewed_candidate),
        discovery_provenance=provenance,
        discovery_reason=record.discovery_reason,
        original_confidence=record.original_confidence,
        human_decision=HumanDecision(decision.human_decision),
        reviewer=reviewer,
        decision_reason=decision.decision_reason,
        resulting_requirement=_requirement_snapshot(record.resulting_requirement),
        decided_at=decision.decided_at,
    )


def _to_candidate(
    record: RequirementCandidateRecord,
    signals: list[LearningSignal],
) -> RequirementCandidate:
    snapshot = SourceDocumentSnapshot.model_validate(record.source_document)
    return RequirementCandidate(
        id=record.id,
        title=record.title,
        description=record.description,
        due_date=record.due_date,
        target=record.target,
        owner=_person(record.owner_snapshot),
        supervisor=_person(record.supervisor_snapshot),
        evidence_expectation=record.evidence_expectation,
        provenance=RequirementProvenance(record.provenance),
        classification=record.classification,
        source_document=SourceDocument.model_validate(snapshot.model_dump()),
        source_document_snapshot=snapshot,
        source_location=_location(record.source_location),
        discovery_reason=record.discovery_reason,
        confidence=record.confidence,
        review_status=ReviewStatus(record.review_status),
        reviewed_by=_person(record.reviewed_by_snapshot),
        reviewed_at=record.reviewed_at,
        review_note=record.review_note,
        original_discovery=CandidateSnapshot.model_validate(record.original_discovery),
        learning_signals=signals,
    )


def _candidate_record(
    candidate: RequirementCandidate,
    organization_id: UUID,
) -> RequirementCandidateRecord:
    record = RequirementCandidateRecord(
        id=candidate.id,
        organization_id=organization_id,
        provenance=candidate.provenance.value,
        classification=candidate.classification.value,
        source_document=_dump(candidate.source_document_snapshot),
        source_location=_dump(candidate.source_location),
        original_discovery=_dump(candidate.original_discovery),
        discovery_reason=candidate.discovery_reason,
        confidence=candidate.confidence,
        review_status=candidate.review_status.value,
    )
    _apply_candidate_edits(record, candidate)
    return record


def _apply_candidate_edits(
    record: RequirementCandidateRecord,
    candidate: RequirementCandidate,
) -> None:
    record.title = candidate.title
    record.description = candidate.description
    record.due_date = candidate.due_date
    record.target = candidate.target
    record.evidence_expectation = candidate.evidence_expectation
    record.owner_snapshot = _dump(candidate.owner and _person_snapshot(candidate.owner))
    record.supervisor_snapshot = _dump(
        candidate.supervisor and _person_snapshot(candidate.supervisor)
    )
    record.classification = candidate.classification.value
    record.review_status = candidate.review_status.value
    record.reviewed_by_snapshot = _dump(
        candidate.reviewed_by and _person_snapshot(candidate.reviewed_by)
    )
    record.reviewed_at = candidate.reviewed_at
    record.review_note = candidate.review_note


def _decision_record(
    signal: LearningSignal,
    organization_id: UUID,
) -> CandidateReviewDecisionRecord:
    if signal.reviewer.id is None:
        raise PersistenceError("reviewer is required")
    return CandidateReviewDecisionRecord(
        id=signal.decision_id,
        organization_id=organization_id,
        candidate_id=signal.candidate_id,
        human_decision=signal.human_decision.value,
        reviewer_user_id=signal.reviewer.id,
        reviewer_snapshot=_dump(signal.reviewer),
        decision_reason=signal.decision_reason,
        decided_at=signal.decided_at,
    )


def _signal_record(
    signal: LearningSignal,
    organization_id: UUID,
) -> LearningSignalRecord:
    return LearningSignalRecord(
        id=signal.id,
        organization_id=organization_id,
        decision_id=signal.decision_id,
        candidate_id=signal.candidate_id,
        source_document=_dump(signal.source_document),
        source_location=_dump(signal.source_location),
        original_discovery=_dump(signal.original_discovery),
        reviewed_candidate=_dump(signal.reviewed_candidate),
        discovery_provenance=(
            None
            if signal.discovery_provenance is None
            else signal.discovery_provenance.value
        ),
        discovery_reason=signal.discovery_reason,
        original_confidence=signal.original_confidence,
        resulting_requirement=_dump(signal.resulting_requirement),
    )


def _refuse_provenance_changes(
    record: RequirementCandidateRecord,
    candidate: RequirementCandidate,
) -> None:
    if (
        record.provenance != candidate.provenance.value
        or record.discovery_reason != candidate.discovery_reason
        or record.confidence != candidate.confidence
        or not _json_same(
            SourceDocumentSnapshot,
            record.source_document,
            candidate.source_document_snapshot,
        )
        or not _json_same(
            SourceLocation,
            record.source_location,
            candidate.source_location,
        )
        or not _json_same(
            CandidateSnapshot,
            record.original_discovery,
            candidate.original_discovery,
        )
    ):
        raise PersistenceError(_PROVENANCE_OVERWRITE)


def _refuse_decision_changes(
    existing: CandidateReviewDecisionRecord,
    incoming: CandidateReviewDecisionRecord,
) -> None:
    if (
        existing.organization_id != incoming.organization_id
        or existing.candidate_id != incoming.candidate_id
        or existing.human_decision != incoming.human_decision
        or existing.reviewer_user_id != incoming.reviewer_user_id
        or existing.decision_reason != incoming.decision_reason
        or existing.decided_at != incoming.decided_at
        or _canonical(existing.reviewer_snapshot)
        != _canonical(incoming.reviewer_snapshot)
    ):
        raise PersistenceError(_DECISION_OVERWRITE)


def _refuse_signal_changes(
    existing: LearningSignalRecord,
    incoming: LearningSignalRecord,
) -> None:
    if (
        existing.organization_id != incoming.organization_id
        or existing.decision_id != incoming.decision_id
        or existing.candidate_id != incoming.candidate_id
        or existing.discovery_provenance != incoming.discovery_provenance
        or existing.discovery_reason != incoming.discovery_reason
        or existing.original_confidence != incoming.original_confidence
        or _canonical(existing.source_document) != _canonical(incoming.source_document)
        or _canonical(existing.source_location) != _canonical(incoming.source_location)
        or _canonical(existing.original_discovery)
        != _canonical(incoming.original_discovery)
        or _canonical(existing.reviewed_candidate)
        != _canonical(incoming.reviewed_candidate)
        or _canonical(existing.resulting_requirement)
        != _canonical(incoming.resulting_requirement)
    ):
        raise PersistenceError(_SIGNAL_OVERWRITE)


def _require_feedback_for_terminal_review(candidate: RequirementCandidate) -> None:
    if candidate.review_status == ReviewStatus.PENDING_REVIEW:
        return
    expected = candidate.review_status.value
    if not any(signal.human_decision.value == expected for signal in candidate.learning_signals):
        raise PersistenceError("a review decision requires its learning signal")


def _person_snapshot(person: Person) -> PersonSnapshot:
    return PersonSnapshot.from_person(person)


def _person(value: dict[str, Any] | None) -> Person | None:
    if value is None:
        return None
    return Person.model_validate(value)


def _location(value: dict[str, Any] | None) -> SourceLocation | None:
    if value is None:
        return None
    return SourceLocation.model_validate(value)


def _candidate_snapshot(value: dict[str, Any] | None) -> CandidateSnapshot | None:
    if value is None:
        return None
    return CandidateSnapshot.model_validate(value)


def _requirement_snapshot(
    value: dict[str, Any] | None,
) -> RequirementResultSnapshot | None:
    if value is None:
        return None
    return RequirementResultSnapshot.model_validate(value)


def _dump(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    return value.model_dump(mode="json")  # type: ignore[attr-defined]


def _json_same(model_type: type, stored: object, incoming: object) -> bool:
    if stored is None or incoming is None:
        return stored is None and incoming is None
    return _canonical(model_type.model_validate(stored).model_dump(mode="json")) == (  # type: ignore[attr-defined]
        _canonical(incoming.model_dump(mode="json"))  # type: ignore[attr-defined]
    )


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
