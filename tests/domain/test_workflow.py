import pytest

from app.domain import (
    DomainValidationError,
    InvalidTransitionError,
    Person,
    Workflow,
    WorkflowStatus,
    WorkflowType,
)
from tests.domain.conftest import make_requirement


def test_creating_a_valid_workflow() -> None:
    workflow = Workflow.create(
        name="FY26 Community Health Grant",
        workflow_type=WorkflowType.GRANT,
    )

    assert workflow.name == "FY26 Community Health Grant"
    assert workflow.workflow_type == WorkflowType.GRANT
    assert workflow.status == WorkflowStatus.DRAFT
    assert workflow.requirements == []


def test_workflow_requires_a_name() -> None:
    with pytest.raises(DomainValidationError, match="name is required"):
        Workflow.create(name="  ", workflow_type=WorkflowType.COMPLIANCE)


def test_workflow_containing_requirements(owner: Person) -> None:
    workflow = Workflow.create(
        name="Vendor MSA renewal",
        workflow_type=WorkflowType.VENDOR_CONTRACT,
    )
    requirement = make_requirement(owner)

    workflow.add_requirement(requirement)

    assert len(workflow.requirements) == 1
    assert workflow.requirements[0].id == requirement.id
    assert workflow.requirements[0].owner == owner


def test_cannot_add_requirements_to_a_closed_workflow(owner: Person) -> None:
    workflow = Workflow.create(name="Closed effort", workflow_type=WorkflowType.GENERAL)
    workflow.activate()
    workflow.complete()

    with pytest.raises(InvalidTransitionError, match="closed workflow"):
        workflow.add_requirement(make_requirement(owner))


def test_invalid_workflow_status_transitions() -> None:
    workflow = Workflow.create(name="Draft only", workflow_type=WorkflowType.GENERAL)

    with pytest.raises(InvalidTransitionError, match="cannot complete"):
        workflow.complete()

    workflow.activate()
    workflow.cancel()

    with pytest.raises(InvalidTransitionError, match="cannot activate"):
        workflow.activate()
