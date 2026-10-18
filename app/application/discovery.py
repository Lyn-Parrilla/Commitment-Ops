"""Use cases for durable discovery review and validated feedback."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from uuid import UUID

from app.application.exceptions import (
    CandidateNotFoundError,
    LearningSignalNotFoundError,
)
from app.domain.discovery import (
    CandidateRevision,
    DiscoveryClassification,
    DocumentType,
    LearningSignal,
    RequirementCandidate,
    SourceDocument,
    SourceLocation,
)
from app.domain.enums import RequirementProvenance
from app.domain.models import Person, Requirement
from app.persistence.discovery_repository import DiscoveryRepository


class CandidateReviewAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class DiscoveryService:
    """Review candidates with the authenticated reviewer and server clock."""

    def __init__(
        self,
        repository: DiscoveryRepository,
        *,
        organization_id: UUID,
        user_id: UUID,
        reviewer: Person,
    ) -> None:
        self._repository = repository
        self._organization_id = organization_id
        self._user_id = user_id
        self._reviewer = reviewer

    def discover_candidate(
        self,
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
        candidate = RequirementCandidate.discover(
            source_document=source_document,
            provenance=provenance,
            title=title,
            description=description,
            due_date=due_date,
            target=target,
            owner=owner,
            supervisor=supervisor,
            evidence_expectation=evidence_expectation,
            classification=classification,
            source_location=source_location,
            discovery_reason=discovery_reason,
            confidence=confidence,
        )
        self._save(candidate)
        return candidate

    def revise_candidate(
        self,
        candidate_id: UUID,
        revision: CandidateRevision,
    ) -> RequirementCandidate:
        candidate = self.get_candidate(candidate_id)
        candidate.revise(revision)
        self._save(candidate)
        return candidate

    def review_candidate(
        self,
        candidate_id: UUID,
        action: CandidateReviewAction,
        *,
        note: str | None = None,
        reason: str | None = None,
    ) -> RequirementCandidate:
        candidate = self.get_candidate(candidate_id)
        if action == CandidateReviewAction.APPROVE:
            candidate.approve(reviewer=self._reviewer, note=note)
        else:
            candidate.reject(reviewer=self._reviewer, reason=reason or "")
        self._save(candidate)
        return candidate

    def record_human_added(
        self,
        *,
        source_document: SourceDocument,
        requirement: Requirement,
        source_location: SourceLocation | None = None,
        reason: str | None = None,
    ) -> LearningSignal:
        signal = LearningSignal.record_human_added(
            source_document=source_document,
            requirement=requirement,
            reviewer=self._reviewer,
            source_location=source_location,
            reason=reason,
        )
        self._repository.save_human_added(
            signal,
            organization_id=self._organization_id,
            user_id=self._user_id,
        )
        return signal

    def get_candidate(self, candidate_id: UUID) -> RequirementCandidate:
        candidate = self._repository.get_candidate(
            candidate_id,
            organization_id=self._organization_id,
            user_id=self._user_id,
        )
        if candidate is None:
            raise CandidateNotFoundError(candidate_id)
        return candidate

    def get_signal(self, signal_id: UUID) -> LearningSignal:
        signal = self._repository.get_signal(
            signal_id,
            organization_id=self._organization_id,
            user_id=self._user_id,
        )
        if signal is None:
            raise LearningSignalNotFoundError(signal_id)
        return signal

    def _save(self, candidate: RequirementCandidate) -> None:
        self._repository.save_candidate(
            candidate,
            organization_id=self._organization_id,
            user_id=self._user_id,
        )
