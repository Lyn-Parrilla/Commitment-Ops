"""Application use cases orchestrating domain rules and durable persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID

from app.application.exceptions import (
    RequirementNotFoundError,
    WorkflowNotFoundError,
)
from app.domain import (
    Person,
    Requirement,
    RequirementProvenance,
    Workflow,
    WorkflowStatus,
    WorkflowType,
)
from app.persistence import WorkflowRepository


class WorkflowTransition(StrEnum):
    ACTIVATE = "activate"
    COMPLETE = "complete"
    CANCEL = "cancel"


class RequirementTransition(StrEnum):
    START_WORK = "start_work"
    BLOCK = "block"
    COMPLETE = "complete"
    CANCEL = "cancel"


class RequirementReviewAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    RESUBMIT = "resubmit"


@dataclass(frozen=True)
class AddRequirementCommand:
    title: str
    description: str
    owner: Person
    evidence_expectation: str
    provenance: RequirementProvenance
    due_date: date | None = None
    target: str | None = None
    supervisor: Person | None = None


class WorkflowService:
    """Focused use cases for the authoritative workflow aggregate."""

    def __init__(
        self,
        repository: WorkflowRepository,
        *,
        organization_id: UUID,
        actor_reference: str,
        reviewer: Person,
    ) -> None:
        self._repository = repository
        self._organization_id = organization_id
        self._actor_reference = actor_reference
        self._reviewer = reviewer

    def create_workflow(
        self, *, name: str, workflow_type: WorkflowType
    ) -> Workflow:
        workflow = Workflow.create(
            name=name,
            workflow_type=workflow_type,
            organization_id=self._organization_id,
        )
        self._save(workflow)
        return workflow

    def list_workflows(
        self,
        *,
        status: WorkflowStatus | None = None,
        workflow_type: WorkflowType | None = None,
    ) -> list[Workflow]:
        return self._repository.list(
            organization_id=self._organization_id,
            status=status,
            workflow_type=workflow_type,
        )

    def get_workflow(self, workflow_id: UUID) -> Workflow:
        workflow = self._repository.get(
            workflow_id,
            organization_id=self._organization_id,
        )
        if workflow is None:
            raise WorkflowNotFoundError(workflow_id)
        return workflow

    def add_requirement(
        self,
        workflow_id: UUID,
        command: AddRequirementCommand,
    ) -> Requirement:
        workflow = self.get_workflow(workflow_id)
        requirement = Requirement.create(
            title=command.title,
            description=command.description,
            owner=command.owner,
            supervisor=command.supervisor,
            evidence_expectation=command.evidence_expectation,
            provenance=command.provenance,
            due_date=command.due_date,
            target=command.target,
        )
        workflow.add_requirement(requirement)
        self._save(workflow)
        return requirement

    def transition_workflow(
        self,
        workflow_id: UUID,
        transition: WorkflowTransition,
    ) -> Workflow:
        workflow = self.get_workflow(workflow_id)
        operations = {
            WorkflowTransition.ACTIVATE: workflow.activate,
            WorkflowTransition.COMPLETE: workflow.complete,
            WorkflowTransition.CANCEL: workflow.cancel,
        }
        operations[transition]()
        self._save(workflow)
        return workflow

    def transition_requirement(
        self,
        workflow_id: UUID,
        requirement_id: UUID,
        transition: RequirementTransition,
    ) -> Requirement:
        workflow = self.get_workflow(workflow_id)
        requirement = _find_requirement(workflow, requirement_id)
        operations = {
            RequirementTransition.START_WORK: requirement.start_work,
            RequirementTransition.BLOCK: requirement.block,
            RequirementTransition.COMPLETE: requirement.complete,
            RequirementTransition.CANCEL: requirement.cancel,
        }
        operations[transition]()
        self._save(workflow)
        return requirement

    def review_requirement(
        self,
        workflow_id: UUID,
        requirement_id: UUID,
        action: RequirementReviewAction,
        *,
        note: str | None = None,
        reason: str | None = None,
    ) -> Requirement:
        workflow = self.get_workflow(workflow_id)
        requirement = _find_requirement(workflow, requirement_id)

        if action == RequirementReviewAction.APPROVE:
            requirement.approve(reviewer=self._reviewer, note=note)
        elif action == RequirementReviewAction.REJECT:
            requirement.reject(reviewer=self._reviewer, reason=reason or "")
        else:
            requirement.submit_for_review()

        self._save(workflow)
        return requirement

    def _save(self, workflow: Workflow) -> None:
        self._repository.save(
            workflow,
            actor_reference=self._actor_reference,
        )


def _find_requirement(workflow: Workflow, requirement_id: UUID) -> Requirement:
    for requirement in workflow.requirements:
        if requirement.id == requirement_id:
            return requirement
    raise RequirementNotFoundError(requirement_id)
