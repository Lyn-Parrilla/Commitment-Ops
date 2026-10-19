"""Use cases for detecting, deciding, and measuring assistance proposals."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.application.exceptions import (
    AssistanceProposalNotFoundError,
    WorkflowNotFoundError,
)
from app.domain.assistance import ProposalDecision, detect_conditions
from app.persistence.assistance_repository import (
    AssistanceRepository,
    StoredProposal,
)
from app.persistence.repository import WorkflowRepository


class AssistanceService:
    """Detect factual conditions and record an authorized human lifecycle."""

    def __init__(
        self,
        workflows: WorkflowRepository,
        proposals: AssistanceRepository,
        *,
        organization_id: UUID,
        user_id: UUID,
    ) -> None:
        self._workflows = workflows
        self._proposals = proposals
        self._organization_id = organization_id
        self._user_id = user_id

    def detect(self, workflow_id: UUID) -> list[StoredProposal]:
        workflow = self._workflow(workflow_id)
        now = datetime.now(UTC)
        conditions = detect_conditions(workflow, today=now.date())
        return self._proposals.ensure_detected(
            conditions,
            organization_id=self._organization_id,
            workflow_id=workflow_id,
            user_id=self._user_id,
            created_at=now,
        )

    def list_proposals(self, workflow_id: UUID) -> list[StoredProposal]:
        self._workflow(workflow_id)
        return self._proposals.list_for_workflow(
            workflow_id,
            organization_id=self._organization_id,
            user_id=self._user_id,
        )

    def get_proposal(
        self,
        workflow_id: UUID,
        proposal_id: UUID,
    ) -> StoredProposal:
        self._workflow(workflow_id)
        return self._required(workflow_id, proposal_id)

    def decide(
        self,
        workflow_id: UUID,
        proposal_id: UUID,
        decision: ProposalDecision,
    ) -> StoredProposal:
        self._workflow(workflow_id)
        stored = self._proposals.decide(
            proposal_id,
            decision,
            organization_id=self._organization_id,
            workflow_id=workflow_id,
            user_id=self._user_id,
            decided_at=datetime.now(UTC),
        )
        if stored is None:
            raise AssistanceProposalNotFoundError(proposal_id)
        return stored

    def execute(self, workflow_id: UUID, proposal_id: UUID) -> StoredProposal:
        self._workflow(workflow_id)
        now = datetime.now(UTC)
        stored = self._proposals.execute(
            proposal_id,
            organization_id=self._organization_id,
            workflow_id=workflow_id,
            user_id=self._user_id,
            executed_at=now,
            today=now.date(),
        )
        if stored is None:
            raise AssistanceProposalNotFoundError(proposal_id)
        return stored

    def _workflow(self, workflow_id: UUID):
        workflow = self._workflows.get(
            workflow_id,
            organization_id=self._organization_id,
            user_id=self._user_id,
        )
        if workflow is None:
            raise WorkflowNotFoundError(workflow_id)
        return workflow

    def _required(self, workflow_id: UUID, proposal_id: UUID) -> StoredProposal:
        stored = self._proposals.get(
            proposal_id,
            organization_id=self._organization_id,
            workflow_id=workflow_id,
            user_id=self._user_id,
        )
        if stored is None:
            raise AssistanceProposalNotFoundError(proposal_id)
        return stored
