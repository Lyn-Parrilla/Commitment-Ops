import inspect
from datetime import date
from uuid import UUID

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
    Organization,
    OrganizationAdmin,
    OrganizationRepository,
    PersistenceError,
    WorkflowRepository,
    create_database_engine,
    create_session_factory,
)
from app.persistence.rls import tenant_session

TEST_ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")


def test_runtime_transactions_cannot_disable_tenant_enforcement() -> None:
    assert "enforce" not in inspect.signature(tenant_session).parameters
    assert not hasattr(OrganizationRepository, "save")
    assert not hasattr(OrganizationRepository, "set_membership")
    with pytest.raises(TypeError):
        tenant_session(None, write=True, enforce=False)  # type: ignore[call-arg]


def test_persist_and_reload_complete_workflow_after_new_repository_instance(
    database_url: str,
    persisted_workflow: Workflow,
) -> None:
    first_engine = create_database_engine(database_url)
    Base.metadata.create_all(first_engine)
    first_session_factory = create_session_factory(first_engine)
    OrganizationAdmin(first_session_factory).save(
        Organization(
            id=TEST_ORGANIZATION_ID,
            name="Synthetic Test Organization",
        )
    )
    WorkflowRepository(first_session_factory).save(
        persisted_workflow,
        actor_reference="synthetic-reviewer",
        correlation_id="synthetic-intake-001",
    )
    first_engine.dispose()

    restarted_engine = create_database_engine(database_url)
    restarted_repository = WorkflowRepository(
        create_session_factory(restarted_engine)
    )
    loaded = _get(restarted_repository, persisted_workflow.id)
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
    assert _get(repository, persisted_workflow.id) is None


def test_reads_and_audit_history_are_organization_scoped(
    repository: WorkflowRepository,
    persisted_workflow: Workflow,
) -> None:
    repository.save(persisted_workflow)
    other_organization_id = UUID("10000000-0000-0000-0000-000000000002")

    assert (
        repository.get(
            persisted_workflow.id,
            organization_id=other_organization_id,
        )
        is None
    )
    assert repository.list(organization_id=other_organization_id) == []
    assert (
        repository.list_audit_events(
            persisted_workflow.id,
            organization_id=other_organization_id,
        )
        == []
    )


def test_workflow_cannot_move_between_organizations(
    repository: WorkflowRepository,
    persisted_workflow: Workflow,
) -> None:
    repository.save(persisted_workflow)
    persisted_workflow.organization_id = UUID(
        "10000000-0000-0000-0000-000000000002"
    )

    with pytest.raises(InvalidAssociationError, match="cannot move"):
        repository.save(persisted_workflow)

    loaded = _get(repository, persisted_workflow.id)
    assert loaded is not None
    assert loaded.organization_id == TEST_ORGANIZATION_ID
    assert len(_events(repository, persisted_workflow.id)) == 1


def test_list_workflows_filters_by_status_and_type(
    repository: WorkflowRepository,
) -> None:
    active_grant = Workflow.create(
        name="Active grant",
        workflow_type=WorkflowType.GRANT,
        organization_id=TEST_ORGANIZATION_ID,
    )
    active_grant.activate()
    draft_contract = Workflow.create(
        name="Draft contract",
        workflow_type=WorkflowType.VENDOR_CONTRACT,
        organization_id=TEST_ORGANIZATION_ID,
    )
    repository.save(active_grant)
    repository.save(draft_contract)

    assert [item.id for item in _list(repository, status=WorkflowStatus.ACTIVE)] == [
        active_grant.id
    ]
    assert [
        item.id
        for item in _list(
            repository,
            workflow_type=WorkflowType.VENDOR_CONTRACT
        )
    ] == [draft_contract.id]
    assert [item.name for item in _list(repository)] == [
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

    reloaded_first = _get(repository, first.id)
    assert reloaded_first is not None
    assert reloaded_first.requirements[0].owner == shared_person
    assert _get(repository, second.id) is None
    assert len(_events(repository, first.id)) == 1
    assert _events(repository, second.id) == []


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

    reloaded_first = _get(repository, first.id)
    reloaded_second = _get(repository, second.id)
    assert reloaded_first is not None
    assert reloaded_second is not None
    first_owner = reloaded_first.requirements[0].owner
    second_owner = reloaded_second.requirements[0].owner
    assert first_owner == shared_person
    assert second_owner == shared_person
    assert first_owner.id == second_owner.id
    assert len(_events(repository, first.id)) == 1
    assert len(_events(repository, second.id)) == 1


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
    original_events = _events(repository, second.id)

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

    reloaded_first = _get(repository, first.id)
    reloaded_second = _get(repository, second.id)
    assert reloaded_first is not None
    assert reloaded_second is not None
    assert reloaded_first.requirements[0].owner == shared_person
    assert reloaded_second.name == "Second workflow"
    assert reloaded_second.status == WorkflowStatus.DRAFT
    assert reloaded_second.requirements[0].owner == shared_person
    assert _events(repository, second.id) == original_events


def test_duplicate_requirement_association_is_rejected_atomically(
    repository: WorkflowRepository,
    persisted_workflow: Workflow,
) -> None:
    requirement = persisted_workflow.requirements[0]
    invalid = Workflow(
        id=persisted_workflow.id,
        name=persisted_workflow.name,
        workflow_type=persisted_workflow.workflow_type,
        organization_id=persisted_workflow.organization_id,
        status=persisted_workflow.status,
        requirements=[requirement, requirement],
    )

    with pytest.raises(InvalidAssociationError, match="duplicate requirement"):
        repository.save(invalid)

    assert _get(repository, invalid.id) is None
    assert _events(repository, invalid.id) == []


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
        organization_id=TEST_ORGANIZATION_ID,
    )
    workflow.add_requirement(first)
    workflow.add_requirement(second)

    with pytest.raises(InvalidAssociationError, match="duplicate evidence"):
        repository.save(workflow)

    assert _get(repository, workflow.id) is None


def test_constraint_failure_rolls_back_workflow_and_audit(
    repository: WorkflowRepository,
    persisted_workflow: Workflow,
) -> None:
    with pytest.raises(PersistenceError, match="persistence failed"):
        repository.save(
            persisted_workflow,
            actor_reference="x" * 256,
        )

    assert _get(repository, persisted_workflow.id) is None
    assert _events(repository, persisted_workflow.id) == []


def test_audit_events_are_append_only_snapshots_of_successful_saves(
    repository: WorkflowRepository,
    persisted_workflow: Workflow,
) -> None:
    repository.save(
        persisted_workflow,
        actor_reference="synthetic-reviewer",
        correlation_id="correlation-001",
    )
    first_event = _events(repository, persisted_workflow.id)[0]

    persisted_workflow.complete()
    repository.save(
        persisted_workflow,
        actor_reference="synthetic-manager",
        correlation_id="correlation-002",
    )
    events = _events(repository, persisted_workflow.id)

    assert len(events) == 2
    assert events[0] == first_event
    assert events[0].event_type == "workflow.created"
    assert events[0].before_state is None
    assert events[0].after_state["status"] == WorkflowStatus.ACTIVE
    assert events[0].actor_reference == "synthetic-reviewer"
    assert events[1].event_type == "workflow.completed"
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

    loaded = _get(repository, persisted_workflow.id)
    assert loaded is not None
    assert loaded.status == WorkflowStatus.ACTIVE
    assert len(_events(repository, persisted_workflow.id)) == 1


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
        organization_id=TEST_ORGANIZATION_ID,
    )
    workflow.add_requirement(pending)

    repository.save(workflow)
    loaded = _get(repository, workflow.id)

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
    loaded = _get(repository, persisted_workflow.id)
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

    loaded = _get(repository, persisted_workflow.id)
    assert loaded is not None
    assert len(loaded.requirements) == 1
    assert len(_events(repository, persisted_workflow.id)) == 1


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
        organization_id=TEST_ORGANIZATION_ID,
    )
    workflow.add_requirement(_requirement(owner, f"{name} requirement"))
    return workflow


def _get(repository: WorkflowRepository, workflow_id: UUID) -> Workflow | None:
    return repository.get(
        workflow_id,
        organization_id=TEST_ORGANIZATION_ID,
    )


def _list(repository: WorkflowRepository, **filters) -> list[Workflow]:
    return repository.list(
        organization_id=TEST_ORGANIZATION_ID,
        **filters,
    )


def _events(repository: WorkflowRepository, workflow_id: UUID):
    return repository.list_audit_events(
        workflow_id,
        organization_id=TEST_ORGANIZATION_ID,
    )
