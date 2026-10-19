"""Deterministic assistance proposals and their measured outcomes.

A proposal, the human decision, execution, and the measured outcome are
separate facts. Detection uses persisted requirement status and due dates.
It does not infer motivation or change who owns the work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.domain.enums import RequirementStatus
from app.domain.exceptions import InvalidTransitionError
from app.domain.models import Requirement, Workflow

_CLOSED = {RequirementStatus.COMPLETED, RequirementStatus.CANCELLED}


class RecommendationType(StrEnum):
    OVERDUE_REQUIREMENT = "overdue_requirement"
    BLOCKED_REQUIREMENT = "blocked_requirement"


class ProposalState(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISMISSED = "dismissed"
    EXECUTED = "executed"


class ProposalDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    DISMISS = "dismiss"


class OutcomeResult(StrEnum):
    ACHIEVED = "achieved"
    NOT_ACHIEVED = "not_achieved"
    UNKNOWN = "unknown"


class ProposalEventType(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISMISSED = "dismissed"
    EXECUTED = "executed"
    OUTCOME_RECORDED = "outcome_recorded"


BLOCKED_INTENDED_OUTCOME = "The requirement is no longer blocked."
OVERDUE_INTENDED_OUTCOME = (
    "The requirement is completed, cancelled, or no longer past its due date."
)
_OPEN_STATES = {ProposalState.PROPOSED, ProposalState.APPROVED}


@dataclass(frozen=True)
class RequirementFacts:
    requirement_id: UUID
    status: RequirementStatus
    due_date: date | None


@dataclass(frozen=True)
class DetectedCondition:
    recommendation_type: RecommendationType
    requirement_id: UUID
    rationale: str
    intended_outcome: str
    observed: dict[str, Any]


@dataclass(frozen=True)
class OutcomeMeasurement:
    result: OutcomeResult
    observed: dict[str, Any]


@dataclass(frozen=True)
class ProposalEvent:
    event_type: ProposalEventType
    actor_user_id: UUID
    occurred_at: datetime
    detail: dict[str, Any]


def detect_conditions(
    workflow: Workflow,
    *,
    today: date,
) -> list[DetectedCondition]:
    """Return factual overdue and blocked conditions for one workflow."""

    conditions: list[DetectedCondition] = []
    for requirement in workflow.requirements:
        if requirement.status == RequirementStatus.BLOCKED:
            conditions.append(_blocked(requirement))
        if _is_overdue(requirement, today):
            conditions.append(_overdue(requirement, today))
    return conditions


def measure_outcome(
    recommendation_type: RecommendationType,
    requirement: RequirementFacts | None,
    *,
    today: date,
) -> OutcomeMeasurement:
    """Judge the intended outcome from current requirement facts.

    ``unknown`` means the facts needed for that judgment are unavailable.
    It is not a successful outcome.
    """

    if requirement is None:
        return OutcomeMeasurement(
            result=OutcomeResult.UNKNOWN,
            observed={"requirement_present": False},
        )
    observed = {
        "requirement_present": True,
        "requirement_id": str(requirement.requirement_id),
        "status": requirement.status.value,
        "due_date": (
            requirement.due_date.isoformat() if requirement.due_date else None
        ),
    }
    if recommendation_type == RecommendationType.BLOCKED_REQUIREMENT:
        result = (
            OutcomeResult.ACHIEVED
            if requirement.status != RequirementStatus.BLOCKED
            else OutcomeResult.NOT_ACHIEVED
        )
        return OutcomeMeasurement(result=result, observed=observed)
    if requirement.status in _CLOSED:
        return OutcomeMeasurement(result=OutcomeResult.ACHIEVED, observed=observed)
    if requirement.due_date is None:
        return OutcomeMeasurement(result=OutcomeResult.UNKNOWN, observed=observed)
    if requirement.due_date >= today:
        return OutcomeMeasurement(result=OutcomeResult.ACHIEVED, observed=observed)
    return OutcomeMeasurement(result=OutcomeResult.NOT_ACHIEVED, observed=observed)


class AssistanceProposal:
    """Current proposal state. History is the separate event sequence."""

    def __init__(
        self,
        *,
        proposal_id: UUID,
        organization_id: UUID,
        workflow_id: UUID,
        requirement_id: UUID,
        recommendation_type: RecommendationType,
        rationale: str,
        intended_outcome: str,
        state: ProposalState,
    ) -> None:
        self.id = proposal_id
        self.organization_id = organization_id
        self.workflow_id = workflow_id
        self.requirement_id = requirement_id
        self.recommendation_type = recommendation_type
        self.rationale = rationale
        self.intended_outcome = intended_outcome
        self.state = state

    @property
    def is_open(self) -> bool:
        return self.state in _OPEN_STATES

    def decide(
        self,
        decision: ProposalDecision,
        *,
        actor_user_id: UUID,
        decided_at: datetime,
    ) -> ProposalEvent:
        if decision == ProposalDecision.DISMISS and self.state in _OPEN_STATES:
            self.state = ProposalState.DISMISSED
            event_type = ProposalEventType.DISMISSED
        elif self.state != ProposalState.PROPOSED:
            raise InvalidTransitionError(
                f"cannot {decision.value} a proposal in state {self.state.value}"
            )
        elif decision == ProposalDecision.APPROVE:
            self.state = ProposalState.APPROVED
            event_type = ProposalEventType.APPROVED
        elif decision == ProposalDecision.REJECT:
            self.state = ProposalState.REJECTED
            event_type = ProposalEventType.REJECTED
        else:
            raise InvalidTransitionError(
                f"cannot {decision.value} a proposal in state {self.state.value}"
            )
        return ProposalEvent(
            event_type=event_type,
            actor_user_id=actor_user_id,
            occurred_at=decided_at,
            detail={"decision": decision.value},
        )

    def execute(
        self,
        *,
        actor_user_id: UUID,
        executed_at: datetime,
        measurement: OutcomeMeasurement,
    ) -> tuple[ProposalEvent, ProposalEvent]:
        if self.state != ProposalState.APPROVED:
            raise InvalidTransitionError(
                "only an approved proposal can be executed"
            )
        self.state = ProposalState.EXECUTED
        executed = ProposalEvent(
            event_type=ProposalEventType.EXECUTED,
            actor_user_id=actor_user_id,
            occurred_at=executed_at,
            detail={"requirement_id": str(self.requirement_id)},
        )
        outcome = ProposalEvent(
            event_type=ProposalEventType.OUTCOME_RECORDED,
            actor_user_id=actor_user_id,
            occurred_at=executed_at,
            detail={
                "result": measurement.result.value,
                "observed": measurement.observed,
            },
        )
        return executed, outcome


def _blocked(requirement: Requirement) -> DetectedCondition:
    return DetectedCondition(
        recommendation_type=RecommendationType.BLOCKED_REQUIREMENT,
        requirement_id=requirement.id,
        rationale=f"Requirement {requirement.id} has status blocked.",
        intended_outcome=BLOCKED_INTENDED_OUTCOME,
        observed=_observed(requirement),
    )


def _overdue(requirement: Requirement, today: date) -> DetectedCondition:
    due = requirement.due_date.isoformat() if requirement.due_date else None
    return DetectedCondition(
        recommendation_type=RecommendationType.OVERDUE_REQUIREMENT,
        requirement_id=requirement.id,
        rationale=(
            f"Requirement {requirement.id} has status "
            f"{requirement.status.value} and due date {due}, "
            f"which is before {today.isoformat()}."
        ),
        intended_outcome=OVERDUE_INTENDED_OUTCOME,
        observed=_observed(requirement),
    )


def _is_overdue(requirement: Requirement, today: date) -> bool:
    return (
        requirement.status not in _CLOSED
        and requirement.due_date is not None
        and requirement.due_date < today
    )


def _observed(requirement: Requirement) -> dict[str, Any]:
    return {
        "requirement_present": True,
        "requirement_id": str(requirement.id),
        "status": requirement.status.value,
        "due_date": (
            requirement.due_date.isoformat() if requirement.due_date else None
        ),
    }
