"""Meaningful workflow events are appended to the existing audit history."""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.domain import (
    Evidence,
    EvidenceType,
    InvalidTransitionError,
    Person,
    Requirement,
    RequirementProvenance,
    Workflow,
    WorkflowStatus,
    WorkflowType,
)
from app.persistence import PersistenceError, WorkflowRepository
from tests.persistence.conftest import TEST_ORGANIZATION_ID
from tests.persistence.postgres_support import (
    LEGACY_ORGANIZATION_ID,
    SKIP_REASON,
    explicit_test_database_url,
    isolated_postgres_engine,
)

OTHER_ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000002")
ACTOR_ID = UUID("20000000-0000-0000-0000-000000000001")


def test_supported_operations_record_meaningful_events(
    repository: WorkflowRepository,
) -> None:
    owner = _person("Alex Rivera", "alex.rivera@example.org")
    reviewer = _person("Reviewer Example", "reviewer@example.test")
    workflow = _workflow()
    pending = _requirement(owner, provenance=RequirementProvenance.AI_INFERRED)
    approved = _requirement(owner)
    workflow.add_requirement(pending)
    workflow.add_requirement(approved)
    _save(repository, workflow)
    assert [event.event_type for event in _events(repository, workflow)] == [
        "workflow.created"
    ]

    workflow.activate()
    _save(repository, workflow)
    approved.start_work()
    _save(repository, workflow)
    approved.block()
    _save(repository, workflow)
    approved.start_work()
    _save(repository, workflow)
    approved.complete()
    _save(repository, workflow)
    pending.reject(reviewer=reviewer, reason="Needs a source citation.")
    _save(repository, workflow)
    pending.submit_for_review()
    _save(repository, workflow)
    pending.approve(reviewer=reviewer, note="Citation confirmed.")
    _save(repository, workflow)
    pending.cancel()
    _save(repository, workflow)
    added = _requirement(owner)
    workflow.add_requirement(added)
    _save(repository, workflow)
    added.add_evidence(_evidence(owner))
    _save(repository, workflow)
    added.assign_owner(_person("Jordan Lee", "jordan.lee@example.org"))
    _save(repository, workflow)
    added.due_date = date(2026, 11, 2)
    _save(repository, workflow)
    workflow.complete()
    _save(repository, workflow)

    assert [event.event_type for event in _events(repository, workflow)] == [
        "workflow.created",
        "workflow.activated",
        "requirement.started",
        "requirement.blocked",
        "requirement.started",
        "requirement.completed",
        "requirement.rejected",
        "requirement.resubmitted",
        "requirement.approved",
        "requirement.cancelled",
        "requirement.added",
        "evidence.added",
        "requirement.owner_changed",
        "requirement.due_date_changed",
        "workflow.completed",
    ]


def test_cancelled_draft_records_workflow_cancelled(
    repository: WorkflowRepository,
) -> None:
    workflow = _workflow()
    _save(repository, workflow)
    workflow.cancel()
    _save(repository, workflow)

    cancelled = _events(repository, workflow)[-1]
    assert cancelled.event_type == "workflow.cancelled"
    assert cancelled.before_state["status"] == WorkflowStatus.DRAFT
    assert cancelled.after_state["status"] == WorkflowStatus.CANCELLED


def test_rejected_transition_does_not_append_an_event(
    repository: WorkflowRepository,
) -> None:
    workflow = _workflow()
    requirement = _requirement(
        _person("Alex Rivera", "alex.rivera@example.org"),
        provenance=RequirementProvenance.AI_INFERRED,
    )
    workflow.add_requirement(requirement)
    _save(repository, workflow)

    with pytest.raises(InvalidTransitionError):
        requirement.start_work()
    with pytest.raises(InvalidTransitionError):
        workflow.complete()

    assert [event.event_type for event in _events(repository, workflow)] == [
        "workflow.created"
    ]


def test_unchanged_save_appends_no_event(
    repository: WorkflowRepository,
) -> None:
    workflow = _workflow()
    workflow.add_requirement(_requirement(_person("Alex Rivera", "alex.rivera@example.org")))
    _save(repository, workflow)
    original = _events(repository, workflow)

    _save(repository, workflow)

    assert _events(repository, workflow) == original
    assert [event.event_type for event in original] == ["workflow.created"]


def test_supervisor_change_records_workflow_updated(
    repository: WorkflowRepository,
) -> None:
    owner = _person("Alex Rivera", "alex.rivera@example.org")
    supervisor = _person("Jordan Lee", "jordan.lee@example.org")
    workflow = _workflow()
    requirement = _requirement(owner)
    workflow.add_requirement(requirement)
    _save(repository, workflow)
    created = _events(repository, workflow)[0]

    requirement.assign_supervisor(supervisor)
    _save(repository, workflow, actor_reference=str(ACTOR_ID))

    history = _events(repository, workflow)
    assert history[0] == created
    assert [event.event_type for event in history] == [
        "workflow.created",
        "workflow.updated",
    ]
    updated = history[1]
    assert updated.actor_reference == str(ACTOR_ID)
    before_supervisor = updated.before_state["requirements"][0]["supervisor_id"]
    after_supervisor = updated.after_state["requirements"][0]["supervisor_id"]
    assert before_supervisor is None
    assert after_supervisor == str(supervisor.id)
    assert supervisor.name not in str(updated.after_state)
    assert supervisor.email not in str(updated.after_state)


def test_named_transition_does_not_also_record_workflow_updated(
    repository: WorkflowRepository,
) -> None:
    workflow = _workflow()
    _save(repository, workflow)
    workflow.activate()
    _save(repository, workflow)

    assert [event.event_type for event in _events(repository, workflow)] == [
        "workflow.created",
        "workflow.activated",
    ]


def test_failed_save_rolls_back_the_event_with_the_state(
    repository: WorkflowRepository,
) -> None:
    workflow = _workflow()
    _save(repository, workflow)
    created = _events(repository, workflow)[0]
    workflow.activate()

    with pytest.raises(PersistenceError, match="persistence failed"):
        repository.save(
            workflow,
            organization_id=TEST_ORGANIZATION_ID,
            correlation_id="x" * 256,
        )

    loaded = repository.get(workflow.id, organization_id=TEST_ORGANIZATION_ID)
    assert loaded is not None
    assert loaded.status == WorkflowStatus.DRAFT
    assert _events(repository, workflow) == [created]


def test_actor_and_occurrence_time_come_from_the_server(
    repository: WorkflowRepository,
) -> None:
    owner = _person("Alex Rivera", "alex.rivera@example.org")
    workflow = _workflow()
    requirement = _requirement(owner)
    evidence = Evidence.create(
        description="Dated register",
        evidence_type=EvidenceType.DOCUMENT,
        submitted_by=owner,
        submitted_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    requirement.add_evidence(evidence)
    workflow.add_requirement(requirement)
    _save(repository, workflow, actor_reference=str(ACTOR_ID))
    before = datetime.now(UTC)
    requirement.due_date = date(2026, 12, 1)
    _save(repository, workflow, actor_reference=str(ACTOR_ID))
    after = datetime.now(UTC)

    created, changed = _events(repository, workflow)
    assert created.actor_reference == str(ACTOR_ID)
    assert changed.actor_reference == str(ACTOR_ID)
    assert changed.event_type == "requirement.due_date_changed"
    assert before - timedelta(seconds=1) <= changed.occurred_at <= after
    assert changed.occurred_at != evidence.submitted_at
    assert "@" not in str(changed.before_state)
    assert "@" not in str(changed.after_state)
    assert changed.after_state["due_date"] == "2026-12-01"
    reloaded = _events(repository, workflow)
    assert [(event.id, event.event_type) for event in reloaded] == [
        (created.id, "workflow.created"),
        (changed.id, "requirement.due_date_changed"),
    ]


def test_other_organization_cannot_read_events_through_the_repository(
    repository: WorkflowRepository,
) -> None:
    workflow = _workflow()
    _save(repository, workflow)

    assert (
        repository.list_audit_events(
            workflow.id,
            organization_id=OTHER_ORGANIZATION_ID,
        )
        == []
    )
    assert len(_events(repository, workflow)) == 1


def test_evidence_event_records_submission_without_evidence_text(
    repository: WorkflowRepository,
) -> None:
    owner = _person("Alex Rivera", "alex.rivera@example.org")
    workflow = _workflow()
    requirement = _requirement(owner)
    workflow.add_requirement(requirement)
    _save(repository, workflow)
    requirement.add_evidence(_evidence(owner))
    _save(repository, workflow)

    added = _events(repository, workflow)[-1]
    assert added.event_type == "evidence.added"
    assert added.after_state["status"] == "submitted"
    assert added.after_state["requirement_id"] == str(requirement.id)
    assert "description" not in added.after_state
    assert "reference" not in added.after_state


@pytest.mark.postgres
def test_postgres_enforces_event_isolation_and_immutability() -> None:
    configured = explicit_test_database_url()
    if configured is None:
        pytest.skip(SKIP_REASON)
    other_organization_id = uuid4()
    workflow_id = uuid4()
    other_workflow_id = uuid4()
    event_id = uuid4()
    with isolated_postgres_engine(configured) as engine:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO organizations (id, name)
                    VALUES (:id, 'Other Synthetic Organization')
                    """
                ),
                {"id": other_organization_id},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO organization_memberships (
                        organization_id, user_id, is_active
                    )
                    VALUES (:organization_id, :user_id, TRUE)
                    """
                ),
                [
                    {
                        "organization_id": LEGACY_ORGANIZATION_ID,
                        "user_id": ACTOR_ID,
                    },
                    {
                        "organization_id": other_organization_id,
                        "user_id": ACTOR_ID,
                    },
                ],
            )
            connection.execute(
                text(
                    """
                    INSERT INTO workflows (
                        id, organization_id, name, workflow_type, status
                    )
                    VALUES
                        (:id, :organization_id, 'Legacy workflow', 'general', 'active'),
                        (
                            :other_id, :other_organization_id,
                            'Other workflow', 'general', 'active'
                        )
                    """
                ),
                {
                    "id": workflow_id,
                    "organization_id": LEGACY_ORGANIZATION_ID,
                    "other_id": other_workflow_id,
                    "other_organization_id": other_organization_id,
                },
            )
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE commitmentops_app"))
            _set_tenant(connection, ACTOR_ID, LEGACY_ORGANIZATION_ID)
            connection.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        id, occurred_at, entity_type, entity_id, event_type,
                        after_state, actor_reference
                    )
                    VALUES (
                        :id, CURRENT_TIMESTAMP, 'workflow', :entity_id,
                        'workflow.completed', CAST(:after_state AS json),
                        :actor_reference
                    )
                    """
                ),
                {
                    "id": event_id,
                    "entity_id": workflow_id,
                    "after_state": '{"status":"completed"}',
                    "actor_reference": str(ACTOR_ID),
                },
            )
            visible = connection.scalar(
                text("SELECT event_type FROM audit_events WHERE id = :id"),
                {"id": event_id},
            )
            assert visible == "workflow.completed"
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE commitmentops_app"))
            _set_tenant(connection, ACTOR_ID, other_organization_id)
            assert (
                connection.scalar(
                    text("SELECT COUNT(*) FROM audit_events WHERE id = :id"),
                    {"id": event_id},
                )
                == 0
            )
            with pytest.raises(SQLAlchemyError):
                connection.execute(
                    text(
                        """
                        INSERT INTO audit_events (
                            id, occurred_at, entity_type, entity_id,
                            event_type, after_state
                        )
                        VALUES (
                            :id, CURRENT_TIMESTAMP, 'workflow', :entity_id,
                            'workflow.cancelled', CAST(:after_state AS json)
                        )
                        """
                    ),
                    {
                        "id": uuid4(),
                        "entity_id": workflow_id,
                        "after_state": '{"status":"cancelled"}',
                    },
                )
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE commitmentops_app"))
            _set_tenant(connection, ACTOR_ID, LEGACY_ORGANIZATION_ID)
            with pytest.raises(SQLAlchemyError):
                connection.execute(
                    text(
                        """
                        UPDATE audit_events
                        SET event_type = 'workflow.updated'
                        WHERE id = :id
                        """
                    ),
                    {"id": event_id},
                )
            with pytest.raises(SQLAlchemyError):
                connection.execute(
                    text("DELETE FROM audit_events WHERE id = :id"),
                    {"id": event_id},
                )


def _workflow() -> Workflow:
    return Workflow.create(
        name="Synthetic workflow",
        workflow_type=WorkflowType.GENERAL,
        organization_id=TEST_ORGANIZATION_ID,
    )


def _requirement(
    owner: Person,
    *,
    provenance: RequirementProvenance = RequirementProvenance.USER_PROVIDED,
) -> Requirement:
    return Requirement.create(
        title="Synthetic requirement",
        description="Record the factual outcome.",
        owner=owner,
        evidence_expectation="A synthetic record",
        provenance=provenance,
        due_date=date(2026, 10, 31),
    )


def _person(name: str, email: str) -> Person:
    return Person.create(name=name, email=email)


def _evidence(owner: Person) -> Evidence:
    return Evidence.create(
        description="Synthetic evidence note",
        evidence_type=EvidenceType.NOTE,
        submitted_by=owner,
        reference="evidence/synthetic.txt",
    )


def _save(
    repository: WorkflowRepository,
    workflow: Workflow,
    *,
    actor_reference: str | None = None,
) -> None:
    repository.save(
        workflow,
        organization_id=TEST_ORGANIZATION_ID,
        actor_reference=actor_reference,
    )


def _events(repository: WorkflowRepository, workflow: Workflow):
    return repository.list_audit_events(
        workflow.id,
        organization_id=TEST_ORGANIZATION_ID,
    )


def _set_tenant(connection, user_id: UUID, organization_id: UUID) -> None:
    connection.execute(
        text("SELECT set_config('app.user_id', :user_id, true)"),
        {"user_id": str(user_id)},
    )
    connection.execute(
        text("SELECT set_config('app.organization_id', :organization_id, true)"),
        {"organization_id": str(organization_id)},
    )
