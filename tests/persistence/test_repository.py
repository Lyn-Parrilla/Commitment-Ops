from datetime import date

import pytest

from app.domain import (
    Evidence,
    EvidenceType,
    InvalidTransitionError,
    Person,
    Requirement,
    RequirementProvenance,
    RequirementStatus,
    ReviewStatus,
    Workflow,
    WorkflowStatus,
    WorkflowType,
)
from app.persistence import (
    Base,
    InvalidAssociationError,
    PersistenceError,
    WorkflowRepository,
    create_database_engine,
    create_session_factory,
)


def test_persist_and_reload_complete_workflow_after_new_repository_instance(
    database_url: str,
    persisted_workflow: Workflow,
) -> None:
    first_engine = create_database_engine(database_url)
    Base.metadata.create_all(first_engine)
    WorkflowRepository(create_session_factory(first_engine)).save(
        persisted_workflow,
        actor_reference="synthetic-reviewer",
        correlation_id="synthetic-intake-001",
    )
    first_engine.dispose()

    restarted_engine = create_database_engine(database_url)
    restarted_repository = WorkflowRepository(
        create_session_factory(restarted_engine)
    )
    loaded = restarted_repository.get(persisted_workflow.id)
    restarted_engine.dispose()

    assert loaded is not None
    assert loaded.model_dump() == persisted_workflow.model_dump()
    requirement = loaded.requirements[0]
    assert requirement.owner.id == persisted_workflow.requirements[0].owner.id
    assert (
        requirement.supervisor.id
        == persisted_workflow.requirements[0].supervisor.id
    )
    assert (
        requirement.reviewed_by.id
        == persisted_workflow.requirements[0].reviewed_by.id
    )
    assert requirement.evidence[0].submitted_by.id == requirement.owner.id


def test_get_missing_workflow_returns_none(
    repository: WorkflowRepository,
    persisted_workflow: Workflow,
) -> None:
    assert repository.get(persisted_workflow.id) is None


def test_list_workflows_filters_by_status_and_type(
    repository: WorkflowRepository,
) -> None:
    active_grant = Workflow.create(
        name="Active grant",
        workflow_type=WorkflowType.GRANT,
    )
    active_grant.activate()
    draft_contract = Workflow.create(
        name="Draft contract",
        workflow_type=WorkflowType.VENDOR_CONTRACT,
    )
    repository.save(active_grant)
    repository.save(draft_contract)

    assert [item.id for item in repository.list(status=WorkflowStatus.ACTIVE)] == [
        active_grant.id
    ]
    assert [
        item.id
        for item in repository.list(
            workflow_type=WorkflowType.VENDOR_CONTRACT
        )
    ] == [draft_contract.id]
    assert [item.name for item in repository.list()] == [
        "Active grant",
        "Draft contract",
    ]


def test_conflicting_shared_person_is_rejected_without_cross_workflow_change(
    repository: WorkflowRepository,
) -> None:
    shared_person = Person.create(
        name="Alex Rivera",
        email="alex.rivera@example.org",
        role="Program Manager",
    )
    first = _workflow_for_owner(shared_person, "First workflow")
    repository.save(first)
    conflicting_person = Person(
        id=shared_person.id,
        name="Changed Name",
        email=shared_person.email,
        role=shared_person.role,
    )
    second = _workflow_for_owner(conflicting_person, "Second workflow")

    with pytest.raises(InvalidAssociationError, match="persisted identity data"):
        repository.save(second)

    reloaded_first = repository.get(first.id)
    assert reloaded_first is not None
    assert reloaded_first.requirements[0].owner == shared_person
    assert repository.get(second.id) is None
    assert len(repository.list_audit_events(first.id)) == 1
    assert repository.list_audit_events(second.id) == []


def test_consistent_shared_person_can_be_referenced_by_multiple_workflows(
    repository: WorkflowRepository,
) -> None:
    shared_person = Person.create(
        name="Alex Rivera",
        email="alex.rivera@example.org",
        role="Program Manager",
    )
    first = _workflow_for_owner(shared_person, "First workflow")
    repository.save(first)
    consistent_reference = shared_person.model_copy(deep=True)
    second = _workflow_for_owner(consistent_reference, "Second workflow")

    repository.save(second)

    reloaded_first = repository.get(first.id)
    reloaded_second = repository.get(second.id)
    assert reloaded_first is not None
    assert reloaded_second is not None
    first_owner = reloaded_first.requirements[0].owner
    second_owner = reloaded_second.requirements[0].owner
    assert first_owner == shared_person
    assert second_owner == shared_person
    assert first_owner.id == second_owner.id
    assert len(repository.list_audit_events(first.id)) == 1
    assert len(repository.list_audit_events(second.id)) == 1


def test_shared_person_conflict_rolls_back_existing_workflow_update(
    repository: WorkflowRepository,
) -> None:
    shared_person = Person.create(
        name="Alex Rivera",
        email="alex.rivera@example.org",
        role="Program Manager",
    )
    first = _workflow_for_owner(shared_person, "First workflow")
    second = _workflow_for_owner(
        shared_person.model_copy(deep=True),
        "Second workflow",
    )
    repository.save(first)
    repository.save(second)
    original_events = repository.list_audit_events(second.id)

    second.name = "Changed second workflow"
    second.activate()
    second.requirements[0].assign_owner(
        Person(
            id=shared_person.id,
            name=shared_person.name,
            email="different-address@example.org",
            role=shared_person.role,
        )
    )

    with pytest.raises(InvalidAssociationError, match="persisted identity data"):
        repository.save(second)

    reloaded_first = repository.get(first.id)
    reloaded_second = repository.get(second.id)
    assert reloaded_first is not None
    assert reloaded_second is not None
    assert reloaded_first.requirements[0].owner == shared_person
    assert reloaded_second.name == "Second workflow"
    assert reloaded_second.status == WorkflowStatus.DRAFT
    assert reloaded_second.requirements[0].owner == shared_person
    assert repository.list_audit_events(second.id) == original_events


def test_duplicate_requirement_association_is_rejected_atomically(
    repository: WorkflowRepository,
    persisted_workflow: Workflow,
) -> None:
    requirement = persisted_workflow.requirements[0]
    invalid = Workflow(
        id=persisted_workflow.id,
        name=persisted_workflow.name,
        workflow_type=persisted_workflow.workflow_type,
        status=persisted_workflow.status,
        requirements=[requirement, requirement],
    )

    with pytest.raises(InvalidAssociationError, match="duplicate requirement"):
        repository.save(invalid)

    assert repository.get(invalid.id) is None
    assert repository.list_audit_events(invalid.id) == []


def test_evidence_cannot_be_associated_with_multiple_requirements(
    repository: WorkflowRepository,
) -> None:
    owner = Person.create(name="Alex Rivera", email="alex.rivera@example.org")
    shared_evidence = Evidence.create(
        description="Shared record",
        evidence_type=EvidenceType.RECORD,
        submitted_by=owner,
    )
    first = _requirement(owner, "First requirement")
    second = _requirement(owner, "Second requirement")
    first.add_evidence(shared_evidence)
    second.add_evidence(shared_evidence)
    workflow = Workflow.create(
        name="Invalid associations",
        workflow_type=WorkflowType.GENERAL,
    )
    workflow.add_requirement(first)
    workflow.add_requirement(second)

    with pytest.raises(InvalidAssociationError, match="duplicate evidence"):
        repository.save(workflow)

    assert repository.get(workflow.id) is None


def test_constraint_failure_rolls_back_workflow_and_audit(
    repository: WorkflowRepository,
    persisted_workflow: Workflow,
) -> None:
    with pytest.raises(PersistenceError, match="persistence failed"):
        repository.save(
            persisted_workflow,
            actor_reference="x" * 256,
        )

    assert repository.get(persisted_workflow.id) is None
    assert repository.list_audit_events(persisted_workflow.id) == []


def test_audit_events_are_append_only_snapshots_of_successful_saves(
    repository: WorkflowRepository,
    persisted_workflow: Workflow,
) -> None:
    repository.save(
        persisted_workflow,
        actor_reference="synthetic-reviewer",
        correlation_id="correlation-001",
    )
    first_event = repository.list_audit_events(persisted_workflow.id)[0]

    persisted_workflow.complete()
    repository.save(
        persisted_workflow,
        actor_reference="synthetic-manager",
        correlation_id="correlation-002",
    )
    events = repository.list_audit_events(persisted_workflow.id)

    assert len(events) == 2
    assert events[0] == first_event
    assert events[0].event_type == "workflow.created"
    assert events[0].before_state is None
    assert events[0].after_state["status"] == WorkflowStatus.ACTIVE
    assert events[0].actor_reference == "synthetic-reviewer"
    assert events[1].event_type == "workflow.updated"
    assert events[1].before_state["status"] == WorkflowStatus.ACTIVE
    assert events[1].after_state["status"] == WorkflowStatus.COMPLETED
    assert events[1].correlation_id == "correlation-002"
    assert not hasattr(repository, "delete_audit_event")


def test_failed_update_rolls_back_state_and_does_not_append_audit(
    repository: WorkflowRepository,
    persisted_workflow: Workflow,
) -> None:
    repository.save(persisted_workflow)
    persisted_workflow.complete()

    with pytest.raises(PersistenceError):
        repository.save(persisted_workflow, correlation_id="x" * 256)

    loaded = repository.get(persisted_workflow.id)
    assert loaded is not None
    assert loaded.status == WorkflowStatus.ACTIVE
    assert len(repository.list_audit_events(persisted_workflow.id)) == 1


def test_unapproved_ai_requirement_remains_unapproved_after_reload(
    repository: WorkflowRepository,
) -> None:
    owner = Person.create(name="Alex Rivera", email="alex.rivera@example.org")
    pending = Requirement.create(
        title="Potential additional report",
        description="Discovery requires human confirmation.",
        owner=owner,
        evidence_expectation="Confirmation from the agreement",
        provenance=RequirementProvenance.AI_INFERRED,
    )
    workflow = Workflow.create(
        name="Pending discovery",
        workflow_type=WorkflowType.GRANT,
    )
    workflow.add_requirement(pending)

    repository.save(workflow)
    loaded = repository.get(workflow.id)

    assert loaded is not None
    loaded_requirement = loaded.requirements[0]
    assert loaded_requirement.review_status == ReviewStatus.PENDING_REVIEW
    assert loaded_requirement.is_authoritative is False
    with pytest.raises(InvalidTransitionError, match="must be approved"):
        loaded_requirement.start_work()


def test_loaded_requirement_retains_domain_lifecycle_behavior(
    repository: WorkflowRepository,
    persisted_workflow: Workflow,
) -> None:
    repository.save(persisted_workflow)
    loaded = repository.get(persisted_workflow.id)
    assert loaded is not None

    loaded.requirements[0].complete()

    assert loaded.requirements[0].status == RequirementStatus.COMPLETED
    with pytest.raises(InvalidTransitionError):
        loaded.requirements[0].start_work()


def test_persisted_workflow_cannot_silently_drop_requirement(
    repository: WorkflowRepository,
    persisted_workflow: Workflow,
) -> None:
    repository.save(persisted_workflow)
    persisted_workflow.requirements.clear()

    with pytest.raises(InvalidAssociationError, match="cannot remove requirements"):
        repository.save(persisted_workflow)

    loaded = repository.get(persisted_workflow.id)
    assert loaded is not None
    assert len(loaded.requirements) == 1
    assert len(repository.list_audit_events(persisted_workflow.id)) == 1


def _requirement(owner: Person, title: str) -> Requirement:
    return Requirement.create(
        title=title,
        description=f"Description for {title}.",
        owner=owner,
        evidence_expectation="Synthetic supporting record",
        provenance=RequirementProvenance.USER_PROVIDED,
        due_date=date(2026, 12, 31),
    )


def _workflow_for_owner(owner: Person, name: str) -> Workflow:
    workflow = Workflow.create(
        name=name,
        workflow_type=WorkflowType.GENERAL,
    )
    workflow.add_requirement(_requirement(owner, f"{name} requirement"))
    return workflow
