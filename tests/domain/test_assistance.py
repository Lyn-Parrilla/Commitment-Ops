"""Deterministic assistance conditions, decisions, and outcomes."""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from app.domain import (
    InvalidTransitionError,
    Person,
    Requirement,
    RequirementProvenance,
    RequirementStatus,
    Workflow,
    WorkflowType,
)
from app.domain.assistance import (
    OutcomeResult,
    ProposalDecision,
    ProposalState,
    RecommendationType,
    RequirementFacts,
    detect_conditions,
    measure_outcome,
)
from app.domain.assistance import AssistanceProposal


def test_detection_reports_overdue_and_blocked_requirements() -> None:
    owner = Person.create(name="Alex Rivera", email="alex.rivera@example.org")
    overdue = _requirement(owner, due_date=date(2026, 10, 1))
    due_today = _requirement(owner, due_date=date(2026, 10, 18))
    blocked = _requirement(owner, due_date=date(2026, 12, 1))
    blocked.start_work()
    blocked.block()
    completed = _requirement(owner, due_date=date(2026, 9, 1))
    completed.complete()
    cancelled = _requirement(owner, due_date=date(2026, 9, 2))
    cancelled.cancel()
    workflow = _workflow(overdue, due_today, blocked, completed, cancelled)

    conditions = detect_conditions(workflow, today=date(2026, 10, 18))

    assert [item.recommendation_type for item in conditions] == [
        RecommendationType.OVERDUE_REQUIREMENT,
        RecommendationType.BLOCKED_REQUIREMENT,
    ]
    assert owner.email not in conditions[0].rationale
    assert owner.name not in conditions[0].rationale
    assert "2026-10-01" in conditions[0].rationale


def test_outcome_distinguishes_measured_results_from_unknown() -> None:
    requirement_id = uuid4()
    blocked = RequirementFacts(
        requirement_id=requirement_id,
        status=RequirementStatus.BLOCKED,
        due_date=date(2026, 10, 1),
    )
    cleared = RequirementFacts(
        requirement_id=requirement_id,
        status=RequirementStatus.IN_PROGRESS,
        due_date=date(2026, 10, 1),
    )
    undated = RequirementFacts(
        requirement_id=requirement_id,
        status=RequirementStatus.NOT_STARTED,
        due_date=None,
    )

    assert (
        measure_outcome(
            RecommendationType.BLOCKED_REQUIREMENT,
            blocked,
            today=date(2026, 10, 18),
        ).result
        == OutcomeResult.NOT_ACHIEVED
    )
    assert (
        measure_outcome(
            RecommendationType.BLOCKED_REQUIREMENT,
            cleared,
            today=date(2026, 10, 18),
        ).result
        == OutcomeResult.ACHIEVED
    )
    overdue = measure_outcome(
        RecommendationType.OVERDUE_REQUIREMENT,
        undated,
        today=date(2026, 10, 18),
    )
    assert overdue.result == OutcomeResult.UNKNOWN
    still_overdue = RequirementFacts(
        requirement_id=requirement_id,
        status=RequirementStatus.IN_PROGRESS,
        due_date=date(2026, 10, 1),
    )
    finished = RequirementFacts(
        requirement_id=requirement_id,
        status=RequirementStatus.COMPLETED,
        due_date=date(2026, 10, 1),
    )
    assert (
        measure_outcome(
            RecommendationType.OVERDUE_REQUIREMENT,
            still_overdue,
            today=date(2026, 10, 18),
        ).result
        == OutcomeResult.NOT_ACHIEVED
    )
    assert (
        measure_outcome(
            RecommendationType.OVERDUE_REQUIREMENT,
            finished,
            today=date(2026, 10, 18),
        ).result
        == OutcomeResult.ACHIEVED
    )
    assert (
        measure_outcome(
            RecommendationType.OVERDUE_REQUIREMENT,
            None,
            today=date(2026, 10, 18),
        ).result
        == OutcomeResult.UNKNOWN
    )


def test_only_an_approved_proposal_can_be_executed_once() -> None:
    proposal = _proposal()
    actor = uuid4()
    now = datetime(2026, 10, 18, tzinfo=UTC)

    with pytest.raises(InvalidTransitionError):
        proposal.execute(
            actor_user_id=actor,
            executed_at=now,
            measurement=measure_outcome(
                proposal.recommendation_type,
                None,
                today=now.date(),
            ),
        )

    proposal.decide(ProposalDecision.APPROVE, actor_user_id=actor, decided_at=now)
    proposal.execute(
        actor_user_id=actor,
        executed_at=now,
        measurement=measure_outcome(
            proposal.recommendation_type,
            None,
            today=now.date(),
        ),
    )

    assert proposal.state == ProposalState.EXECUTED
    with pytest.raises(InvalidTransitionError):
        proposal.execute(
            actor_user_id=actor,
            executed_at=now,
            measurement=measure_outcome(
                proposal.recommendation_type,
                None,
                today=now.date(),
            ),
        )


def test_rejected_and_dismissed_proposals_are_not_executed() -> None:
    actor = uuid4()
    now = datetime(2026, 10, 18, tzinfo=UTC)
    rejected = _proposal()
    rejected.decide(ProposalDecision.REJECT, actor_user_id=actor, decided_at=now)
    dismissed = _proposal()
    dismissed.decide(ProposalDecision.APPROVE, actor_user_id=actor, decided_at=now)
    dismissed.decide(ProposalDecision.DISMISS, actor_user_id=actor, decided_at=now)

    for proposal in (rejected, dismissed):
        with pytest.raises(InvalidTransitionError):
            proposal.execute(
                actor_user_id=actor,
                executed_at=now,
                measurement=measure_outcome(
                    proposal.recommendation_type,
                    None,
                    today=now.date(),
                ),
            )


def _proposal() -> AssistanceProposal:
    return AssistanceProposal(
        proposal_id=uuid4(),
        organization_id=uuid4(),
        workflow_id=uuid4(),
        requirement_id=uuid4(),
        recommendation_type=RecommendationType.BLOCKED_REQUIREMENT,
        rationale="Requirement is blocked.",
        intended_outcome="The requirement is no longer blocked.",
        state=ProposalState.PROPOSED,
    )


def _workflow(*requirements: Requirement) -> Workflow:
    workflow = Workflow.create(
        name="Synthetic workflow",
        workflow_type=WorkflowType.GENERAL,
        organization_id=uuid4(),
    )
    for requirement in requirements:
        workflow.add_requirement(requirement)
    return workflow


def _requirement(owner: Person, *, due_date: date) -> Requirement:
    return Requirement.create(
        title="Synthetic requirement",
        description="Record the factual outcome.",
        owner=owner,
        evidence_expectation="A synthetic record",
        provenance=RequirementProvenance.USER_PROVIDED,
        due_date=due_date,
    )
