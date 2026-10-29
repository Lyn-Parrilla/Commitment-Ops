"""Assistance proposals survive reload and roll back with failed writes."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Barrier, Thread
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.domain import (
    Person,
    Requirement,
    RequirementProvenance,
    Workflow,
    WorkflowType,
)
from app.domain.assistance import (
    DetectedCondition,
    ProposalDecision,
    ProposalEventType,
    ProposalState,
    RecommendationType,
)
from app.persistence import (
    AssistanceRepository,
    Base,
    Organization,
    OrganizationAdmin,
    PersistenceError,
    WorkflowRepository,
    create_database_engine,
    create_session_factory,
)
from app.persistence.database import SessionFactory
from app.persistence.orm import AssistanceProposalEventRecord
from app.persistence.database import quoted_isolated_schema
from app.persistence.orm import AssistanceProposalRecord
from tests.persistence.postgres_support import (
    LEGACY_ORGANIZATION_ID,
    SKIP_REASON,
    assert_postgres_test_url,
    create_test_login,
    explicit_test_database_url,
    isolated_postgres_engine,
    require_test_target,
    temporary_role_url,
)

ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
OTHER_ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000002")
USER_ID = UUID("20000000-0000-0000-0000-000000000001")
DETECT_LOGIN = "commitmentops_detect"
_DETECT_PASSWORD: str | None = None


@pytest.fixture
def stores(tmp_path: Path) -> Iterator[tuple[WorkflowRepository, AssistanceRepository, SessionFactory]]:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'assistance.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    OrganizationAdmin(factory).save(
        Organization(id=ORGANIZATION_ID, name="Synthetic Organization")
    )
    OrganizationAdmin(factory).save(
        Organization(id=OTHER_ORGANIZATION_ID, name="Other Synthetic Organization")
    )
    yield WorkflowRepository(factory), AssistanceRepository(factory), factory
    engine.dispose()


def test_proposal_decision_and_outcome_reload_without_rewriting_history(
    stores: tuple[WorkflowRepository, AssistanceRepository, SessionFactory],
) -> None:
    workflows, proposals, _factory = stores
    workflow, requirement = _blocked_workflow()
    workflows.save(workflow, organization_id=ORGANIZATION_ID, user_id=USER_ID)
    created = proposals.ensure_detected(
        _blocked_condition(requirement),
        organization_id=ORGANIZATION_ID,
        workflow_id=workflow.id,
        user_id=USER_ID,
        created_at=datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
    )[0]
    again = proposals.ensure_detected(
        _blocked_condition(requirement),
        organization_id=ORGANIZATION_ID,
        workflow_id=workflow.id,
        user_id=USER_ID,
        created_at=datetime(2026, 10, 18, 13, 0, tzinfo=UTC),
    )

    assert [item.id for item in again] == [created.id]
    approved = proposals.decide(
        created.id,
        ProposalDecision.APPROVE,
        organization_id=ORGANIZATION_ID,
        workflow_id=workflow.id,
        user_id=USER_ID,
        decided_at=datetime(2026, 10, 18, 14, 0, tzinfo=UTC),
    )
    assert approved is not None
    executed = proposals.execute(
        created.id,
        organization_id=ORGANIZATION_ID,
        workflow_id=workflow.id,
        user_id=USER_ID,
        executed_at=datetime(2026, 10, 18, 15, 0, tzinfo=UTC),
        today=date(2026, 10, 18),
    )
    assert executed is not None
    requirement.start_work()
    workflows.save(workflow, organization_id=ORGANIZATION_ID, user_id=USER_ID)
    reloaded = proposals.get(
        created.id,
        organization_id=ORGANIZATION_ID,
        workflow_id=workflow.id,
        user_id=USER_ID,
    )

    assert reloaded is not None
    assert reloaded.state == ProposalState.EXECUTED
    assert [event.event_type for event in reloaded.events] == [
        ProposalEventType.PROPOSED,
        ProposalEventType.APPROVED,
        ProposalEventType.EXECUTED,
        ProposalEventType.OUTCOME_RECORDED,
    ]
    assert reloaded.events[0].id == created.events[0].id
    assert reloaded.events[0].detail == created.events[0].detail
    assert reloaded.events[1].actor_user_id == USER_ID
    assert reloaded.events[-1].detail["result"] == "not_achieved"
    assert "@" not in reloaded.rationale


def test_failed_execution_rolls_back_the_proposal_state(
    stores: tuple[WorkflowRepository, AssistanceRepository, SessionFactory],
) -> None:
    workflows, proposals, factory = stores
    workflow, requirement = _blocked_workflow()
    workflows.save(workflow, organization_id=ORGANIZATION_ID, user_id=USER_ID)
    created = proposals.ensure_detected(
        _blocked_condition(requirement),
        organization_id=ORGANIZATION_ID,
        workflow_id=workflow.id,
        user_id=USER_ID,
        created_at=datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
    )[0]
    proposals.decide(
        created.id,
        ProposalDecision.APPROVE,
        organization_id=ORGANIZATION_ID,
        workflow_id=workflow.id,
        user_id=USER_ID,
        decided_at=datetime(2026, 10, 18, 14, 0, tzinfo=UTC),
    )
    with factory.begin() as session:
        session.add(
            AssistanceProposalEventRecord(
                id=uuid4(),
                organization_id=ORGANIZATION_ID,
                proposal_id=created.id,
                event_type=ProposalEventType.OUTCOME_RECORDED.value,
                actor_user_id=USER_ID,
                occurred_at=datetime(2026, 10, 18, 14, 30, tzinfo=UTC),
                detail={"result": "unknown"},
            )
        )

    with pytest.raises(PersistenceError, match="persistence failed"):
        proposals.execute(
            created.id,
            organization_id=ORGANIZATION_ID,
            workflow_id=workflow.id,
            user_id=USER_ID,
            executed_at=datetime(2026, 10, 18, 15, 0, tzinfo=UTC),
            today=date(2026, 10, 18),
        )

    reloaded = proposals.get(
        created.id,
        organization_id=ORGANIZATION_ID,
        workflow_id=workflow.id,
        user_id=USER_ID,
    )
    assert reloaded is not None
    assert reloaded.state == ProposalState.APPROVED
    assert ProposalEventType.EXECUTED not in [
        event.event_type for event in reloaded.events
    ]


def test_terminal_proposal_allows_a_new_detection(
    stores: tuple[WorkflowRepository, AssistanceRepository, SessionFactory],
) -> None:
    workflows, proposals, _factory = stores
    workflow, requirement = _blocked_workflow()
    workflows.save(workflow, organization_id=ORGANIZATION_ID, user_id=USER_ID)
    condition = _blocked_condition(requirement)
    created = _detect(proposals, workflow.id, condition)
    again = _detect(proposals, workflow.id, condition)

    assert again.id == created.id

    current = created
    for decision in (
        ProposalDecision.REJECT,
        ProposalDecision.DISMISS,
        ProposalDecision.APPROVE,
    ):
        if decision == ProposalDecision.APPROVE:
            proposals.decide(
                current.id,
                decision,
                organization_id=ORGANIZATION_ID,
                workflow_id=workflow.id,
                user_id=USER_ID,
                decided_at=datetime(2026, 10, 18, 14, 0, tzinfo=UTC),
            )
            proposals.execute(
                current.id,
                organization_id=ORGANIZATION_ID,
                workflow_id=workflow.id,
                user_id=USER_ID,
                executed_at=datetime(2026, 10, 18, 15, 0, tzinfo=UTC),
                today=date(2026, 10, 18),
            )
        else:
            proposals.decide(
                current.id,
                decision,
                organization_id=ORGANIZATION_ID,
                workflow_id=workflow.id,
                user_id=USER_ID,
                decided_at=datetime(2026, 10, 18, 14, 0, tzinfo=UTC),
            )
        detected = _detect(proposals, workflow.id, condition)
        assert detected.id != current.id
        assert detected.state == ProposalState.PROPOSED
        current = detected


def test_other_organization_cannot_read_a_proposal(
    stores: tuple[WorkflowRepository, AssistanceRepository, SessionFactory],
) -> None:
    workflows, proposals, _factory = stores
    workflow, requirement = _blocked_workflow()
    workflows.save(workflow, organization_id=ORGANIZATION_ID, user_id=USER_ID)
    created = proposals.ensure_detected(
        _blocked_condition(requirement),
        organization_id=ORGANIZATION_ID,
        workflow_id=workflow.id,
        user_id=USER_ID,
        created_at=datetime(2026, 10, 18, tzinfo=UTC),
    )[0]

    assert (
        proposals.get(
            created.id,
            organization_id=OTHER_ORGANIZATION_ID,
            workflow_id=workflow.id,
            user_id=USER_ID,
        )
        is None
    )


@pytest.mark.postgres
def test_postgres_rejects_cross_organization_proposal_access() -> None:
    configured = explicit_test_database_url()
    if configured is None:
        pytest.skip(SKIP_REASON)
    other_organization_id = uuid4()
    workflow_id = uuid4()
    other_workflow_id = uuid4()
    requirement_id = uuid4()
    other_requirement_id = uuid4()
    proposal_id = uuid4()
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
                        "user_id": USER_ID,
                    },
                    {
                        "organization_id": other_organization_id,
                        "user_id": USER_ID,
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
            connection.execute(
                text(
                    """
                    INSERT INTO people (id, name, email)
                    VALUES (:id, 'Synthetic Owner', 'owner@example.org')
                    """
                ),
                {"id": USER_ID},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO requirements (
                        id, workflow_id, title, description, owner_id,
                        evidence_expectation, provenance, status, review_status
                    )
                    VALUES
                        (
                            :id, :workflow_id, 'Blocked', 'Synthetic', :owner_id,
                            'A record', 'user_provided', 'blocked', 'approved'
                        ),
                        (
                            :other_requirement_id, :other_workflow_id, 'Other',
                            'Synthetic', :owner_id, 'A record', 'user_provided',
                            'blocked', 'approved'
                        )
                    """
                ),
                {
                    "id": requirement_id,
                    "workflow_id": workflow_id,
                    "other_requirement_id": other_requirement_id,
                    "other_workflow_id": other_workflow_id,
                    "owner_id": USER_ID,
                },
            )
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE commitmentops_app"))
            _set_tenant(connection, USER_ID, LEGACY_ORGANIZATION_ID)
            connection.execute(
                text(
                    """
                    INSERT INTO assistance_proposals (
                        id, organization_id, workflow_id, requirement_id,
                        recommendation_type, rationale, intended_outcome,
                        lifecycle_state, created_at
                    )
                    VALUES (
                        :id, :organization_id, :workflow_id, :requirement_id,
                        'blocked_requirement', 'Requirement is blocked.',
                        'The requirement is no longer blocked.',
                        'approved', CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "id": proposal_id,
                    "organization_id": LEGACY_ORGANIZATION_ID,
                    "workflow_id": workflow_id,
                    "requirement_id": requirement_id,
                },
            )
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE commitmentops_app"))
            _set_tenant(connection, USER_ID, other_organization_id)
            assert (
                connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM assistance_proposals WHERE id = :id"
                    ),
                    {"id": proposal_id},
                )
                == 0
            )
            with pytest.raises(SQLAlchemyError):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            """
                            INSERT INTO assistance_proposal_events (
                                id, organization_id, proposal_id, event_type,
                                actor_user_id, occurred_at, detail
                            )
                            VALUES (
                                :id, :organization_id, :proposal_id, 'executed',
                                :actor_user_id, CURRENT_TIMESTAMP,
                                CAST(:detail AS json)
                            )
                            """
                        ),
                        {
                            "id": uuid4(),
                            "organization_id": other_organization_id,
                            "proposal_id": proposal_id,
                            "actor_user_id": USER_ID,
                            "detail": "{}",
                        },
                    )
            hidden_update = connection.execute(
                text(
                    """
                    UPDATE assistance_proposals
                    SET lifecycle_state = 'executed'
                    WHERE id = :id
                    """
                ),
                {"id": proposal_id},
            )
            assert hidden_update.rowcount == 0
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE commitmentops_app"))
            _set_tenant(connection, USER_ID, LEGACY_ORGANIZATION_ID)
            event_id = uuid4()
            connection.execute(
                text(
                    """
                    INSERT INTO assistance_proposal_events (
                        id, organization_id, proposal_id, event_type,
                        actor_user_id, occurred_at, detail
                    )
                    VALUES (
                        :id, :organization_id, :proposal_id, 'approved',
                        :actor_user_id, CURRENT_TIMESTAMP, CAST('{}' AS json)
                    )
                    """
                ),
                {
                    "id": event_id,
                    "organization_id": LEGACY_ORGANIZATION_ID,
                    "proposal_id": proposal_id,
                    "actor_user_id": USER_ID,
                },
            )
            with pytest.raises(SQLAlchemyError):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            """
                            UPDATE assistance_proposals
                            SET rationale = 'rewritten'
                            WHERE id = :id
                            """
                        ),
                        {"id": proposal_id},
                    )
            with pytest.raises(SQLAlchemyError):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            """
                            UPDATE assistance_proposal_events
                            SET detail = CAST('{"changed": true}' AS json)
                            WHERE id = :id
                            """
                        ),
                        {"id": event_id},
                    )
            with pytest.raises(SQLAlchemyError):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            "DELETE FROM assistance_proposal_events WHERE id = :id"
                        ),
                        {"id": event_id},
                    )
            stored_detail = connection.scalar(
                text(
                    "SELECT detail::text FROM assistance_proposal_events "
                    "WHERE id = :id"
                ),
                {"id": event_id},
            )
            assert stored_detail == "{}"


@pytest.mark.postgres
def test_postgres_rejects_cross_organization_proposal_references() -> None:
    configured = explicit_test_database_url()
    if configured is None:
        pytest.skip(SKIP_REASON)
    other_organization_id = uuid4()
    workflow_id = uuid4()
    other_workflow_id = uuid4()
    requirement_id = uuid4()
    other_requirement_id = uuid4()
    proposal_id = uuid4()
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
            connection.execute(
                text(
                    """
                    INSERT INTO people (id, name, email)
                    VALUES (:id, 'Synthetic Owner', 'owner@example.org')
                    """
                ),
                {"id": USER_ID},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO requirements (
                        id, workflow_id, title, description, owner_id,
                        evidence_expectation, provenance, status, review_status
                    )
                    VALUES
                        (
                            :id, :workflow_id, 'Blocked', 'Synthetic', :owner_id,
                            'A record', 'user_provided', 'blocked', 'approved'
                        ),
                        (
                            :other_requirement_id, :other_workflow_id, 'Other',
                            'Synthetic', :owner_id, 'A record', 'user_provided',
                            'blocked', 'approved'
                        )
                    """
                ),
                {
                    "id": requirement_id,
                    "workflow_id": workflow_id,
                    "other_requirement_id": other_requirement_id,
                    "other_workflow_id": other_workflow_id,
                    "owner_id": USER_ID,
                },
            )
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                _insert_proposal(
                    connection,
                    proposal_id=uuid4(),
                    organization_id=other_organization_id,
                    workflow_id=workflow_id,
                    requirement_id=requirement_id,
                )
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                _insert_proposal(
                    connection,
                    proposal_id=uuid4(),
                    organization_id=LEGACY_ORGANIZATION_ID,
                    workflow_id=workflow_id,
                    requirement_id=other_requirement_id,
                )
        with engine.begin() as connection:
            _insert_proposal(
                connection,
                proposal_id=proposal_id,
                organization_id=LEGACY_ORGANIZATION_ID,
                workflow_id=workflow_id,
                requirement_id=requirement_id,
            )
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO assistance_proposal_events (
                            id, organization_id, proposal_id, event_type,
                            actor_user_id, occurred_at, detail
                        )
                        VALUES (
                            :id, :organization_id, :proposal_id, 'executed',
                            :actor_user_id, CURRENT_TIMESTAMP,
                            CAST('{}' AS json)
                        )
                        """
                    ),
                    {
                        "id": uuid4(),
                        "organization_id": other_organization_id,
                        "proposal_id": proposal_id,
                        "actor_user_id": USER_ID,
                    },
                )


@pytest.mark.postgres
def test_postgres_concurrent_execution_commits_once() -> None:
    configured = explicit_test_database_url()
    if configured is None:
        pytest.skip(SKIP_REASON)
    workflow_id = uuid4()
    requirement_id = uuid4()
    proposal_id = uuid4()
    with isolated_postgres_engine(configured) as engine:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO organization_memberships (
                        organization_id, user_id, is_active
                    )
                    VALUES (:organization_id, :user_id, TRUE)
                    """
                ),
                {
                    "organization_id": LEGACY_ORGANIZATION_ID,
                    "user_id": USER_ID,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO workflows (
                        id, organization_id, name, workflow_type, status
                    )
                    VALUES (
                        :id, :organization_id, 'Legacy workflow',
                        'general', 'active'
                    )
                    """
                ),
                {
                    "id": workflow_id,
                    "organization_id": LEGACY_ORGANIZATION_ID,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO people (id, name, email)
                    VALUES (:id, 'Synthetic Owner', 'owner@example.org')
                    """
                ),
                {"id": USER_ID},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO requirements (
                        id, workflow_id, title, description, owner_id,
                        evidence_expectation, provenance, status, review_status
                    )
                    VALUES (
                        :id, :workflow_id, 'Blocked', 'Synthetic', :owner_id,
                        'A record', 'user_provided', 'blocked', 'approved'
                    )
                    """
                ),
                {
                    "id": requirement_id,
                    "workflow_id": workflow_id,
                    "owner_id": USER_ID,
                },
            )
            _insert_proposal(
                connection,
                proposal_id=proposal_id,
                organization_id=LEGACY_ORGANIZATION_ID,
                workflow_id=workflow_id,
                requirement_id=requirement_id,
            )
            connection.execute(
                text(
                    """
                    UPDATE assistance_proposals
                    SET lifecycle_state = 'approved'
                    WHERE id = :id
                    """
                ),
                {"id": proposal_id},
            )
        barrier = Barrier(2)
        results: list[str] = []

        def attempt() -> None:
            outcome = "rejected"
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    connection.execute(text("SET LOCAL ROLE commitmentops_app"))
                    _set_tenant(connection, USER_ID, LEGACY_ORGANIZATION_ID)
                    state = connection.scalar(
                        text(
                            """
                            SELECT lifecycle_state
                            FROM assistance_proposals
                            WHERE id = :id
                            """
                        ),
                        {"id": proposal_id},
                    )
                    barrier.wait(timeout=5)
                    if state != "approved":
                        transaction.rollback()
                        outcome = "not-approved"
                        return
                    connection.execute(
                        text(
                            """
                            UPDATE assistance_proposals
                            SET lifecycle_state = 'executed'
                            WHERE id = :id
                            """
                        ),
                        {"id": proposal_id},
                    )
                    _insert_execution_event(
                        connection,
                        proposal_id=proposal_id,
                        event_type="executed",
                    )
                    _insert_execution_event(
                        connection,
                        proposal_id=proposal_id,
                        event_type="outcome_recorded",
                    )
                    transaction.commit()
                    outcome = "committed"
                except Exception:
                    transaction.rollback()
                    outcome = "rejected"
            results.append(outcome)

        workers = [Thread(target=attempt) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        with engine.connect() as connection:
            state = connection.scalar(
                text(
                    """
                    SELECT lifecycle_state
                    FROM assistance_proposals
                    WHERE id = :id
                    """
                ),
                {"id": proposal_id},
            )
            executed_events = connection.scalar(
                text(
                    """
                    SELECT COUNT(*)
                    FROM assistance_proposal_events
                    WHERE proposal_id = :id AND event_type = 'executed'
                    """
                ),
                {"id": proposal_id},
            )

    assert sorted(results) == ["committed", "rejected"]
    assert state == "executed"
    assert executed_events == 1


@pytest.mark.postgres
def test_postgres_concurrent_detection_keeps_one_open_proposal() -> None:
    configured = explicit_test_database_url()
    if configured is None:
        pytest.skip(SKIP_REASON)
    url = assert_postgres_test_url(configured)
    admin = create_engine(
        url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"connect_timeout": 5},
    )
    _replace_detect_login(admin)
    workflow_id = uuid4()
    requirement_id = uuid4()
    try:
        with isolated_postgres_engine(url, runtime_login=DETECT_LOGIN) as engine:
            target = require_test_target(engine)
            quoted_schema = quoted_isolated_schema(target.schema)
            with engine.begin() as connection:
                connection.execute(
                    text(f"GRANT USAGE ON SCHEMA {quoted_schema} TO {DETECT_LOGIN}")
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
                    {
                        "organization_id": LEGACY_ORGANIZATION_ID,
                        "user_id": USER_ID,
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO workflows (
                            id, organization_id, name, workflow_type, status
                        )
                        VALUES (
                            :id, :organization_id, 'Legacy workflow',
                            'general', 'active'
                        )
                        """
                    ),
                    {
                        "id": workflow_id,
                        "organization_id": LEGACY_ORGANIZATION_ID,
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO people (id, name, email)
                        VALUES (:id, 'Synthetic Owner', 'owner@example.org')
                        """
                    ),
                    {"id": USER_ID},
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO requirements (
                            id, workflow_id, title, description, owner_id,
                            evidence_expectation, provenance, status,
                            review_status
                        )
                        VALUES (
                            :id, :workflow_id, 'Blocked', 'Synthetic', :owner_id,
                            'A record', 'user_provided', 'blocked', 'approved'
                        )
                        """
                    ),
                    {
                        "id": requirement_id,
                        "workflow_id": workflow_id,
                        "owner_id": USER_ID,
                    },
                )
            runtime = create_database_engine(
                _detect_login_url(url),
                schema=target.schema,
                connect_args={"connect_timeout": 5},
            )
            try:
                repository = AssistanceRepository(create_session_factory(runtime))
                detected: list[UUID] = []
                failures: list[BaseException] = []
                overlap = Barrier(2)

                def pause_overlapping_inserts(
                    session, _flush_context, _instances
                ) -> None:
                    if any(
                        isinstance(obj, AssistanceProposalRecord)
                        for obj in session.new
                    ):
                        overlap.wait(timeout=5)

                event.listen(Session, "before_flush", pause_overlapping_inserts)
                try:

                    def detect() -> None:
                        try:
                            stored = repository.ensure_detected(
                                _blocked_condition(
                                    _requirement_for(requirement_id)
                                ),
                                organization_id=LEGACY_ORGANIZATION_ID,
                                workflow_id=workflow_id,
                                user_id=USER_ID,
                                created_at=datetime(2026, 10, 18, tzinfo=UTC),
                            )
                            detected.append(stored[0].id)
                        except Exception as exc:
                            failures.append(exc)

                    workers = [Thread(target=detect) for _ in range(2)]
                    for worker in workers:
                        worker.start()
                    for worker in workers:
                        worker.join()
                finally:
                    event.remove(Session, "before_flush", pause_overlapping_inserts)
                assert failures == []
                with engine.begin() as connection:
                    open_count = connection.scalar(
                        text(
                            """
                            SELECT COUNT(*)
                            FROM assistance_proposals
                            WHERE workflow_id = :workflow_id
                              AND requirement_id = :requirement_id
                              AND lifecycle_state IN ('proposed', 'approved')
                            """
                        ),
                        {
                            "workflow_id": workflow_id,
                            "requirement_id": requirement_id,
                        },
                    )
                    connection.execute(
                        text(
                            """
                            UPDATE assistance_proposals
                            SET lifecycle_state = 'rejected'
                            WHERE workflow_id = :workflow_id
                            """
                        ),
                        {"workflow_id": workflow_id},
                    )
                replaced = repository.ensure_detected(
                    _blocked_condition(_requirement_for(requirement_id)),
                    organization_id=LEGACY_ORGANIZATION_ID,
                    workflow_id=workflow_id,
                    user_id=USER_ID,
                    created_at=datetime(2026, 10, 18, 1, tzinfo=UTC),
                )[0]
                with engine.connect() as connection:
                    open_after = connection.scalar(
                        text(
                            """
                            SELECT COUNT(*)
                            FROM assistance_proposals
                            WHERE workflow_id = :workflow_id
                              AND lifecycle_state IN ('proposed', 'approved')
                            """
                        ),
                        {"workflow_id": workflow_id},
                    )
                    total = connection.scalar(
                        text(
                            """
                            SELECT COUNT(*)
                            FROM assistance_proposals
                            WHERE workflow_id = :workflow_id
                            """
                        ),
                        {"workflow_id": workflow_id},
                    )
            finally:
                runtime.dispose()
    finally:
        _drop_detect_login(admin)
        admin.dispose()

    assert len(detected) == 2
    assert detected[0] == detected[1]
    assert open_count == 1
    assert replaced.id != detected[0]
    assert replaced.state == ProposalState.PROPOSED
    assert open_after == 1
    assert total == 2


def _requirement_for(requirement_id: UUID) -> Requirement:
    owner = Person.create(name="Alex Rivera", email="alex.rivera@example.org")
    requirement = Requirement.create(
        title="Synthetic requirement",
        description="Record the factual outcome.",
        owner=owner,
        evidence_expectation="A synthetic record",
        provenance=RequirementProvenance.USER_PROVIDED,
        due_date=date(2026, 12, 1),
    )
    requirement.start_work()
    requirement.block()
    return requirement.model_copy(update={"id": requirement_id})


def _detect(
    proposals: AssistanceRepository,
    workflow_id: UUID,
    condition: list[DetectedCondition],
):
    return proposals.ensure_detected(
        condition,
        organization_id=ORGANIZATION_ID,
        workflow_id=workflow_id,
        user_id=USER_ID,
        created_at=datetime(2026, 10, 18, tzinfo=UTC),
    )[0]


def _replace_detect_login(admin) -> None:
    global _DETECT_PASSWORD
    _drop_detect_login(admin)
    with admin.begin() as connection:
        _DETECT_PASSWORD = create_test_login(
            connection,
            DETECT_LOGIN,
            "LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT",
        )


def _drop_detect_login(admin) -> None:
    global _DETECT_PASSWORD
    with admin.begin() as connection:
        exists = connection.scalar(
            text("SELECT 1 FROM pg_roles WHERE rolname = :name"),
            {"name": DETECT_LOGIN},
        )
        if not exists:
            _DETECT_PASSWORD = None
            return
        if connection.scalar(
            text("SELECT pg_has_role(:login, 'commitmentops_app', 'MEMBER')"),
            {"login": DETECT_LOGIN},
        ):
            connection.execute(
                text(f"REVOKE commitmentops_app FROM {DETECT_LOGIN}")
            )
        connection.execute(text(f"DROP ROLE {DETECT_LOGIN}"))
        _DETECT_PASSWORD = None


def _detect_login_url(url: str) -> str:
    if _DETECT_PASSWORD is None:
        raise AssertionError("test login has no password")
    return temporary_role_url(url, DETECT_LOGIN, _DETECT_PASSWORD)


def _insert_execution_event(connection, *, proposal_id: UUID, event_type: str) -> None:
    connection.execute(
        text(
            """
            INSERT INTO assistance_proposal_events (
                id, organization_id, proposal_id, event_type,
                actor_user_id, occurred_at, detail
            )
            VALUES (
                :id, :organization_id, :proposal_id, :event_type,
                :actor_user_id, CURRENT_TIMESTAMP, CAST('{}' AS json)
            )
            """
        ),
        {
            "id": uuid4(),
            "organization_id": LEGACY_ORGANIZATION_ID,
            "proposal_id": proposal_id,
            "event_type": event_type,
            "actor_user_id": USER_ID,
        },
    )


def _insert_proposal(
    connection,
    *,
    proposal_id: UUID,
    organization_id: UUID,
    workflow_id: UUID,
    requirement_id: UUID,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO assistance_proposals (
                id, organization_id, workflow_id, requirement_id,
                recommendation_type, rationale, intended_outcome,
                lifecycle_state, created_at
            )
            VALUES (
                :id, :organization_id, :workflow_id, :requirement_id,
                'blocked_requirement', 'Requirement is blocked.',
                'The requirement is no longer blocked.',
                'proposed', CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": proposal_id,
            "organization_id": organization_id,
            "workflow_id": workflow_id,
            "requirement_id": requirement_id,
        },
    )


def _blocked_workflow() -> tuple[Workflow, Requirement]:
    owner = Person.create(name="Alex Rivera", email="alex.rivera@example.org")
    requirement = Requirement.create(
        title="Synthetic requirement",
        description="Record the factual outcome.",
        owner=owner,
        evidence_expectation="A synthetic record",
        provenance=RequirementProvenance.USER_PROVIDED,
        due_date=date(2026, 12, 1),
    )
    requirement.start_work()
    requirement.block()
    workflow = Workflow.create(
        name="Synthetic workflow",
        workflow_type=WorkflowType.GENERAL,
        organization_id=ORGANIZATION_ID,
    )
    workflow.add_requirement(requirement)
    return workflow, requirement


def _blocked_condition(requirement: Requirement) -> list[DetectedCondition]:
    return [
        DetectedCondition(
            recommendation_type=RecommendationType.BLOCKED_REQUIREMENT,
            requirement_id=requirement.id,
            rationale=f"Requirement {requirement.id} has status blocked.",
            intended_outcome="The requirement is no longer blocked.",
            observed={"status": "blocked"},
        )
    ]


def _set_tenant(connection, user_id: UUID, organization_id: UUID) -> None:
    connection.execute(
        text("SELECT set_config('app.user_id', :user_id, true)"),
        {"user_id": str(user_id)},
    )
    connection.execute(
        text("SELECT set_config('app.organization_id', :organization_id, true)"),
        {"organization_id": str(organization_id)},
    )
