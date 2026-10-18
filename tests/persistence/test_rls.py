"""Prove PostgreSQL policies, not the bypass login, isolate tenants."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.domain import (
    Evidence,
    EvidenceType,
    Person,
    Requirement,
    RequirementProvenance,
    Workflow,
    WorkflowType,
)

from alembic import command
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from app.persistence import (
    Organization,
    OrganizationAdmin,
    OrganizationRepository,
    PersistenceError,
    WorkflowRepository,
    create_database_engine,
    create_session_factory,
)
from app.config import Settings
from app.persistence.database import quoted_isolated_schema
from app.persistence.rls import (
    APP_ROLE,
    RUNTIME_LOGIN_REFUSAL,
    RUNTIME_SESSION_MEMBERSHIP,
    RUNTIME_SESSION_MISMATCH,
    verify_configured_runtime_session,
    verify_runtime_session,
)
from tests.persistence.postgres_support import (
    LEGACY_ORGANIZATION_ID,
    SKIP_REASON,
    IsolatedPostgresTarget,
    _create_schema,
    alembic_config,
    assert_postgres_test_url,
    clear_isolated_tables,
    drop_isolated_schema,
    explicit_test_database_url,
    isolated_postgres_engine,
    new_test_schema,
    require_test_target,
)

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def postgres_engine():
    url = explicit_test_database_url()
    if url is None:
        pytest.skip(SKIP_REASON)
    with isolated_postgres_engine(url) as engine:
        yield engine


@pytest.fixture(autouse=True)
def _clear_isolated_rows(postgres_engine) -> None:
    clear_isolated_tables(postgres_engine)


def test_application_role_is_subject_to_forced_rls(postgres_engine) -> None:
    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(text("SET LOCAL ROLE commitmentops_app"))
        (
            role_name,
            is_superuser,
            bypasses_rls,
            can_create_role,
            can_create_db,
            can_login,
        ) = connection.execute(
            text(
                """
                SELECT
                    rolname,
                    rolsuper,
                    rolbypassrls,
                    rolcreaterole,
                    rolcreatedb,
                    rolcanlogin
                FROM pg_roles
                WHERE rolname = current_user
                """
            )
        ).one()
        forced = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM pg_class
                JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
                WHERE pg_namespace.nspname = current_schema()
                  AND pg_class.relname IN (
                    'organizations', 'organization_memberships', 'people',
                    'workflows', 'requirements', 'evidence', 'audit_events'
                  )
                  AND pg_class.relrowsecurity
                  AND pg_class.relforcerowsecurity
                """
            )
        ).scalar()
        with pytest.raises(SQLAlchemyError):
            with connection.begin_nested():
                connection.execute(text("CREATE TABLE tenant_probe (id integer)"))
        transaction.rollback()

    assert role_name == APP_ROLE
    assert is_superuser is False
    assert bypasses_rls is False
    assert can_create_role is False
    assert can_create_db is False
    assert can_login is False
    assert forced == 7


def test_cross_tenant_statements_fail_closed(postgres_engine) -> None:
    seeded = _seed(postgres_engine)

    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        privileged = connection.scalar(text("SELECT COUNT(*) FROM workflows"))
        assert privileged == 2
        _assume_member(connection, seeded["user_a"], seeded["org_a"])
        assert set(_ids(connection, "workflows")) == {seeded["workflow_a"]}
        assert set(_ids(connection, "requirements")) == {seeded["requirement_a"]}
        assert set(_ids(connection, "evidence")) == {seeded["evidence_a"]}
        assert connection.scalars(
            text("SELECT entity_id FROM audit_events")
        ).all() == [seeded["workflow_a"]]
        assert set(_ids(connection, "organizations")) == {seeded["org_a"]}
        assert connection.scalars(
            text("SELECT user_id FROM organization_memberships")
        ).all() == [seeded["user_a"]]
        assert set(_ids(connection, "people")) == {seeded["user_a"]}

        hidden_update = connection.execute(
            text("UPDATE workflows SET name = 'taken' WHERE id = :workflow_id"),
            {"workflow_id": seeded["workflow_b"]},
        )
        assert hidden_update.rowcount == 0
        with pytest.raises(SQLAlchemyError):
            with connection.begin_nested():
                connection.execute(
                    text(
                        """
                        INSERT INTO workflows (
                            id, organization_id, name, workflow_type, status
                        )
                        VALUES (
                            :id, :organization_id, 'smuggled', 'general', 'draft'
                        )
                        """
                    ),
                    {
                        "id": uuid4(),
                        "organization_id": seeded["org_b"],
                    },
                )
        with pytest.raises(SQLAlchemyError):
            with connection.begin_nested():
                connection.execute(
                    text(
                        """
                        INSERT INTO requirements (
                            id, workflow_id, title, description, owner_id,
                            evidence_expectation, provenance, status, review_status
                        )
                        VALUES (
                            :id, :workflow_id, 'smuggled', 'hidden', :owner_id,
                            'none', 'user_provided', 'not_started', 'pending_review'
                        )
                        """
                    ),
                    {
                        "id": uuid4(),
                        "workflow_id": seeded["workflow_b"],
                        "owner_id": seeded["user_a"],
                    },
                )
        transaction.rollback()

        absent = connection.begin()
        connection.execute(text("SET LOCAL ROLE commitmentops_app"))
        assert connection.scalar(text("SELECT COUNT(*) FROM workflows")) == 0
        assert connection.scalar(text("SELECT COUNT(*) FROM requirements")) == 0
        assert connection.scalar(text("SELECT COUNT(*) FROM evidence")) == 0
        assert connection.scalar(text("SELECT COUNT(*) FROM audit_events")) == 0
        assert connection.scalar(text("SELECT COUNT(*) FROM organizations")) == 0
        assert connection.scalar(text("SELECT COUNT(*) FROM people")) == 0
        absent.rollback()

        wrong_organization = connection.begin()
        _assume_member(connection, seeded["user_a"], seeded["org_b"])
        assert connection.scalar(text("SELECT COUNT(*) FROM workflows")) == 0
        wrong_organization.rollback()

        malformed = connection.begin()
        connection.execute(text("SET LOCAL ROLE commitmentops_app"))
        connection.execute(
            text("SELECT set_config('app.user_id', 'not-a-uuid', true)")
        )
        connection.execute(
            text(
                "SELECT set_config('app.organization_id', :organization_id, true)"
            ),
            {"organization_id": str(seeded["org_a"])},
        )
        assert connection.scalar(text("SELECT COUNT(*) FROM workflows")) == 0
        malformed.rollback()

    with postgres_engine.connect() as connection:
        preserved = connection.scalar(
            text("SELECT name FROM workflows WHERE id = :workflow_id"),
            {"workflow_id": seeded["workflow_b"]},
        )
    assert preserved == "Organization B"


def test_context_does_not_survive_rollback_or_pool_reuse(postgres_engine) -> None:
    seeded = _seed(postgres_engine)

    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        _assume_member(connection, seeded["user_a"], seeded["org_a"])
        assert (
            connection.scalar(text("SELECT current_setting('app.user_id', true)"))
            == str(seeded["user_a"])
        )
        transaction.rollback()
        follow_up = connection.begin()
        assert connection.scalar(
            text("SELECT current_setting('app.user_id', true)")
        ) in (None, "")
        connection.execute(text("SET LOCAL ROLE commitmentops_app"))
        assert connection.scalar(text("SELECT COUNT(*) FROM workflows")) == 0
        follow_up.rollback()

    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        assert connection.scalar(
            text("SELECT current_setting('app.user_id', true)")
        ) in (None, "")
        connection.execute(text("SET LOCAL ROLE commitmentops_app"))
        assert connection.scalar(text("SELECT COUNT(*) FROM workflows")) == 0
        transaction.rollback()


def test_member_can_save_and_reload_through_the_repository(
    postgres_engine,
    persisted_workflow,
) -> None:
    assert persisted_workflow.organization_id is not None
    admin_factory = create_session_factory(postgres_engine)
    organizations = OrganizationAdmin(admin_factory)
    with _request_connection(postgres_engine) as request_engine:
        repository = WorkflowRepository(create_session_factory(request_engine))
        user_id = UUID("20000000-0000-0000-0000-000000000021")
        organizations.save(
            Organization(
                id=persisted_workflow.organization_id,
                name="Synthetic Authorized Organization",
            )
        )
        organizations.set_membership(
            organization_id=persisted_workflow.organization_id,
            user_id=user_id,
            is_active=True,
        )

        repository.save(
            persisted_workflow,
            user_id=user_id,
            organization_id=persisted_workflow.organization_id,
        )
        persisted_workflow.name = "Synthetic renamed workflow"
        repository.save(
            persisted_workflow,
            user_id=user_id,
            organization_id=persisted_workflow.organization_id,
        )
        loaded = repository.get(
            persisted_workflow.id,
            organization_id=persisted_workflow.organization_id,
            user_id=user_id,
        )

        assert loaded is not None
        assert loaded.name == "Synthetic renamed workflow"
        assert (
            loaded.requirements[0].evidence[0].description
            == "Q3 participant register"
        )
        assert repository.list_audit_events(
            persisted_workflow.id,
            organization_id=persisted_workflow.organization_id,
            user_id=user_id,
        )


def test_person_insert_requires_a_visible_workflow_reference(
    postgres_engine,
) -> None:
    org_id, other_org_id = uuid4(), uuid4()
    user_id, other_user_id = uuid4(), uuid4()
    owner = Person.create(name="Alex Rivera", email="alex.rivera@example.org")
    supervisor = Person.create(name="Jordan Lee", email="jordan.lee@example.org")
    reviewer = Person.create(name="Sam Okonkwo", email="sam.okonkwo@example.org")
    submitter = Person.create(name="Casey Ng", email="casey.ng@example.org")
    requirement = Requirement.create(
        title="Submit the quarterly report",
        description="File the narrative.",
        owner=owner,
        supervisor=supervisor,
        evidence_expectation="A record",
        provenance=RequirementProvenance.AI_INFERRED,
    )
    requirement.approve(reviewer=reviewer, note="Confirmed.")
    requirement.add_evidence(
        Evidence.create(
            description="Quarterly register",
            evidence_type=EvidenceType.NOTE,
            submitted_by=submitter,
        )
    )
    workflow = Workflow.create(
        name="Shared people",
        workflow_type=WorkflowType.GENERAL,
        organization_id=org_id,
    )
    workflow.add_requirement(requirement)
    admin = OrganizationAdmin(create_session_factory(postgres_engine))
    admin.save(Organization(id=org_id, name="Authorized organization"))
    admin.save(Organization(id=other_org_id, name="Other organization"))
    admin.set_membership(
        organization_id=org_id, user_id=user_id, is_active=True
    )
    admin.set_membership(
        organization_id=other_org_id, user_id=other_user_id, is_active=True
    )

    with _request_connection(postgres_engine) as request_engine:
        repository = WorkflowRepository(create_session_factory(request_engine))
        repository.save(workflow, user_id=user_id, organization_id=org_id)
        loaded = repository.get(workflow.id, organization_id=org_id, user_id=user_id)
        assert loaded is not None
        assert loaded.requirements[0].owner.id == owner.id
        assert loaded.requirements[0].supervisor is not None
        assert loaded.requirements[0].supervisor.id == supervisor.id
        assert loaded.requirements[0].reviewed_by is not None
        assert loaded.requirements[0].reviewed_by.id == reviewer.id
        assert loaded.requirements[0].evidence[0].submitted_by.id == submitter.id
        assert repository.list_audit_events(
            workflow.id, organization_id=org_id, user_id=user_id
        )

        shared = Workflow.create(
            name="Second workflow",
            workflow_type=WorkflowType.GENERAL,
            organization_id=org_id,
        )
        shared.add_requirement(
            Requirement.create(
                title="Reuse the same owner",
                description="The person remains shared.",
                owner=owner.model_copy(deep=True),
                evidence_expectation="A record",
                provenance=RequirementProvenance.USER_PROVIDED,
            )
        )
        repository.save(shared, user_id=user_id, organization_id=org_id)
        reloaded = repository.get(
            shared.id, organization_id=org_id, user_id=user_id
        )
        assert reloaded is not None
        assert reloaded.requirements[0].owner.id == owner.id
        assert (
            repository.get(workflow.id, organization_id=org_id, user_id=other_user_id)
            is None
        )

        with request_engine.connect() as connection:
            denied = connection.begin()
            _assume_member(connection, user_id, org_id)
            with pytest.raises(SQLAlchemyError, match="row-level security"):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            "INSERT INTO people (id, name, email) "
                            "VALUES (:id, :name, :email)"
                        ),
                        {
                            "id": uuid4(),
                            "name": "Unreferenced",
                            "email": "unreferenced@example.test",
                        },
                    )
            denied.rollback()

            hidden = connection.begin()
            _assume_member(connection, other_user_id, other_org_id)
            assert (
                connection.scalar(
                    text("SELECT COUNT(*) FROM people WHERE id = :id"),
                    {"id": owner.id},
                )
                == 0
            )
            hidden.rollback()

            referenced = connection.begin()
            _assume_member(connection, user_id, org_id)
            person_id, workflow_id, requirement_id = uuid4(), uuid4(), uuid4()
            connection.execute(
                text(
                    """
                    INSERT INTO workflows (
                        id, organization_id, name, workflow_type, status
                    )
                    VALUES (:id, :organization_id, 'Direct', 'general', 'draft')
                    """
                ),
                {"id": workflow_id, "organization_id": org_id},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO requirements (
                        id, workflow_id, title, description, owner_id,
                        evidence_expectation, provenance, status, review_status
                    )
                    VALUES (
                        :id, :workflow_id, 'Direct', 'Referenced', :owner_id,
                        'A record', 'user_provided', 'not_started', 'approved'
                    )
                    """
                ),
                {
                    "id": requirement_id,
                    "workflow_id": workflow_id,
                    "owner_id": person_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO people (id, name, email) "
                    "VALUES (:id, :name, :email)"
                ),
                {
                    "id": person_id,
                    "name": "Referenced",
                    "email": "referenced@example.test",
                },
            )
            assert (
                connection.scalar(
                    text("SELECT COUNT(*) FROM people WHERE id = :id"),
                    {"id": person_id},
                )
                == 1
            )
            referenced.rollback()
            follow_up = connection.begin()
            assert connection.scalar(
                text("SELECT current_setting('app.user_id', true)")
            ) in (None, "")
            connection.execute(text("SET LOCAL ROLE commitmentops_app"))
            assert connection.scalar(text("SELECT COUNT(*) FROM workflows")) == 0
            follow_up.rollback()

        with request_engine.connect() as connection:
            pooled = connection.begin()
            assert connection.scalar(
                text("SELECT current_setting('app.user_id', true)")
            ) in (None, "")
            connection.execute(text("SET LOCAL ROLE commitmentops_app"))
            assert connection.scalar(text("SELECT COUNT(*) FROM people")) == 0
            pooled.rollback()

    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT COUNT(*) FROM people WHERE id = :id"),
                {"id": person_id},
            )
            == 0
        )
        assert (
            connection.scalar(
                text("SELECT COUNT(*) FROM people WHERE id = :id"),
                {"id": owner.id},
            )
            == 1
        )


def test_postgresql_downgrade_keeps_deferred_person_foreign_keys() -> None:
    url = _postgres_url()
    admin = _admin_engine(url)
    schema = new_test_schema()
    quoted = quoted_isolated_schema(schema)
    created = False
    try:
        _create_schema(admin, schema)
        created = True
        target = IsolatedPostgresTarget(url, schema)
        command.upgrade(alembic_config(target), "head")
        with pytest.raises(Exception) as caught:
            command.downgrade(alembic_config(target), "20261016_03")
        assert (
            "Refusing to downgrade because person foreign keys must stay deferred"
            in _messages(caught.value)
        )
        person_id, workflow_id, requirement_id = uuid4(), uuid4(), uuid4()
        with admin.begin() as connection:
            connection.execute(text(f"SET search_path TO {quoted}"))
            version = connection.scalar(text("SELECT version_num FROM alembic_version"))
            deferred = connection.scalar(
                text(
                    """
                    SELECT condeferred AND condeferrable
                    FROM pg_constraint
                    JOIN pg_class ON pg_class.oid = pg_constraint.conrelid
                    JOIN pg_namespace
                      ON pg_namespace.oid = pg_class.relnamespace
                    WHERE pg_namespace.nspname = current_schema()
                      AND pg_class.relname = 'requirements'
                      AND pg_constraint.conname = 'requirements_owner_id_fkey'
                    """
                )
            )
            policy = connection.scalar(
                text(
                    """
                    SELECT 1
                    FROM pg_policies
                    WHERE schemaname = current_schema()
                      AND policyname = 'people_insert_tenant'
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO workflows (
                        id, organization_id, name, workflow_type, status
                    )
                    VALUES (
                        :id, :organization_id, 'Deferred save', 'general', 'draft'
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
                    INSERT INTO requirements (
                        id, workflow_id, title, description, owner_id,
                        evidence_expectation, provenance, status, review_status
                    )
                    VALUES (
                        :id, :workflow_id, 'Deferred', 'Reference first', :owner_id,
                        'A record', 'user_provided', 'not_started', 'approved'
                    )
                    """
                ),
                {
                    "id": requirement_id,
                    "workflow_id": workflow_id,
                    "owner_id": person_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO people (id, name, email) "
                    "VALUES (:id, 'Deferred Person', 'deferred@example.test')"
                ),
                {"id": person_id},
            )
        assert version == "20261018_05"
        assert deferred is True
        assert policy == 1
    finally:
        if created:
            drop_isolated_schema(admin, schema)
        admin.dispose()


def test_distinct_runtime_login_is_subject_to_rls() -> None:
    url = _postgres_url()
    admin = _admin_engine(url)
    try:
        _ensure_role(
            admin,
            RUNTIME_LOGIN,
            "LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT",
        )
        with isolated_postgres_engine(url, runtime_login=RUNTIME_LOGIN) as engine:
            target = require_test_target(engine)
            with engine.begin() as connection:
                assert connection.scalar(
                    text("SELECT pg_has_role(:login, :role, 'MEMBER')"),
                    {"login": RUNTIME_LOGIN, "role": APP_ROLE},
                ) is True
                assert _is_app_member(connection, RUNTIME_LOGIN) is True
                attributes = connection.execute(
                    text(
                        """
                        SELECT rolsuper, rolbypassrls, rolcreaterole,
                               rolcreatedb, rolcanlogin
                        FROM pg_roles
                        WHERE rolname = :login
                        """
                    ),
                    {"login": RUNTIME_LOGIN},
                ).one()
                assert tuple(attributes) == (False, False, False, False, True)
                connection.execute(
                    text(
                        f"GRANT USAGE ON SCHEMA {quoted_isolated_schema(target.schema)} "
                        f"TO {RUNTIME_LOGIN}"
                    )
                )
            seeded = _seed(engine)
            with _dispose_after(
                create_database_engine(
                    _role_url(url, RUNTIME_LOGIN),
                    schema=target.schema,
                    connect_args={"connect_timeout": 5},
                )
            ) as runtime_engine:
                with runtime_engine.connect() as connection:
                    denied = connection.begin()
                    with pytest.raises(SQLAlchemyError):
                        connection.scalar(text("SELECT COUNT(*) FROM workflows"))
                    denied.rollback()

                factory = create_session_factory(runtime_engine)
                repository = WorkflowRepository(factory)
                reads = OrganizationRepository(factory)
                loaded = repository.get(
                    seeded["workflow_a"],
                    organization_id=seeded["org_a"],
                    user_id=seeded["user_a"],
                )
                assert loaded is not None
                assert loaded.requirements[0].id == seeded["requirement_a"]
                assert loaded.requirements[0].evidence[0].id == seeded["evidence_a"]
                assert (
                    repository.get(
                        seeded["workflow_b"],
                        organization_id=seeded["org_b"],
                        user_id=seeded["user_a"],
                    )
                    is None
                )
                assert repository.list_audit_events(
                    seeded["workflow_a"],
                    organization_id=seeded["org_a"],
                    user_id=seeded["user_a"],
                )
                assert (
                    repository.list_audit_events(
                        seeded["workflow_b"],
                        organization_id=seeded["org_b"],
                        user_id=seeded["user_a"],
                    )
                    == []
                )
                assert reads.has_active_membership(
                    organization_id=seeded["org_a"],
                    user_id=seeded["user_a"],
                )
                assert not reads.has_active_membership(
                    organization_id=seeded["org_b"],
                    user_id=seeded["user_a"],
                )
                assert (
                    repository.get(
                        seeded["workflow_a"],
                        organization_id=seeded["org_a"],
                        user_id=None,
                    )
                    is None
                )
                with pytest.raises(
                    PersistenceError,
                    match="organization administration is unavailable",
                ):
                    OrganizationAdmin(factory).save(
                        Organization(id=uuid4(), name="Runtime bootstrap")
                    )

                with runtime_engine.connect() as connection:
                    transaction = connection.begin()
                    _assume_member(connection, seeded["user_a"], seeded["org_a"])
                    hidden_update = connection.execute(
                        text(
                            "UPDATE workflows SET name = 'taken' WHERE id = :workflow_id"
                        ),
                        {"workflow_id": seeded["workflow_b"]},
                    )
                    assert hidden_update.rowcount == 0
                    with pytest.raises(SQLAlchemyError):
                        with connection.begin_nested():
                            connection.execute(
                                text(
                                    """
                                    INSERT INTO requirements (
                                        id, workflow_id, title, description, owner_id,
                                        evidence_expectation, provenance, status,
                                        review_status
                                    )
                                    VALUES (
                                        :id, :workflow_id, 'smuggled', 'hidden',
                                        :owner_id, 'none', 'user_provided',
                                        'not_started', 'pending_review'
                                    )
                                    """
                                ),
                                {
                                    "id": uuid4(),
                                    "workflow_id": seeded["workflow_b"],
                                    "owner_id": seeded["user_a"],
                                },
                            )
                    transaction.rollback()
                    follow_up = connection.begin()
                    assert connection.scalar(
                        text("SELECT current_setting('app.user_id', true)")
                    ) in (None, "")
                    connection.execute(text("SET LOCAL ROLE commitmentops_app"))
                    assert connection.scalar(text("SELECT COUNT(*) FROM workflows")) == 0
                    assert connection.scalar(text("SELECT COUNT(*) FROM evidence")) == 0
                    assert (
                        connection.scalar(text("SELECT COUNT(*) FROM audit_events")) == 0
                    )
                    follow_up.rollback()
                with runtime_engine.connect() as connection:
                    pooled = connection.begin()
                    assert connection.scalar(
                        text("SELECT current_setting('app.user_id', true)")
                    ) in (None, "")
                    connection.execute(text("SET LOCAL ROLE commitmentops_app"))
                    assert connection.scalar(text("SELECT COUNT(*) FROM workflows")) == 0
                    pooled.rollback()
    finally:
        _drop_managed_role(admin, RUNTIME_LOGIN)
        admin.dispose()


def test_runtime_session_preflight_reads_session_user_without_changes() -> None:
    url = _postgres_url()
    admin = _admin_engine(url)
    try:
        _ensure_role(
            admin,
            RUNTIME_LOGIN,
            "LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT",
        )
        _ensure_role(
            admin,
            DENIED_LOGIN,
            "LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT",
        )
        with isolated_postgres_engine(url, runtime_login=RUNTIME_LOGIN) as engine:
            with engine.connect() as connection:
                with pytest.raises(PersistenceError, match=RUNTIME_SESSION_MISMATCH):
                    verify_runtime_session(connection, RUNTIME_LOGIN)
                assert connection.scalar(text("SELECT session_user")) != RUNTIME_LOGIN
            with _dispose_after(
                create_database_engine(
                    _role_url(url, RUNTIME_LOGIN),
                    connect_args={"connect_timeout": 5},
                )
            ) as runtime_engine:
                with runtime_engine.connect() as connection:
                    verify_runtime_session(connection, RUNTIME_LOGIN)
                    assert connection.scalar(text("SELECT session_user")) == RUNTIME_LOGIN
                    assert connection.scalar(text("SELECT current_user")) == RUNTIME_LOGIN
                verify_configured_runtime_session(
                    Settings(
                        _env_file=None,
                        database_url=_role_url(url, RUNTIME_LOGIN),
                    )
                )
            with engine.connect() as connection:
                session_user = connection.scalar(text("SELECT session_user"))
                with pytest.raises(PersistenceError, match=RUNTIME_LOGIN_REFUSAL):
                    verify_runtime_session(connection, str(session_user))
            with _dispose_after(
                create_database_engine(
                    _role_url(url, DENIED_LOGIN),
                    connect_args={"connect_timeout": 5},
                )
            ) as denied_engine:
                with denied_engine.connect() as connection:
                    with pytest.raises(
                        PersistenceError, match=RUNTIME_SESSION_MEMBERSHIP
                    ):
                        verify_runtime_session(connection, DENIED_LOGIN)
                    assert connection.scalar(text("SELECT current_user")) == DENIED_LOGIN
    finally:
        _drop_managed_role(admin, RUNTIME_LOGIN)
        _drop_managed_role(admin, DENIED_LOGIN)
        admin.dispose()


def test_unsafe_preexisting_application_role_is_rejected() -> None:
    url = _postgres_url()
    admin = _admin_engine(url)
    schema = new_test_schema()
    created = False
    try:
        _swap_in_unsafe_application_role(admin)
        _create_schema(admin, schema)
        created = True
        with pytest.raises(Exception) as caught:
            command.upgrade(
                alembic_config(IsolatedPostgresTarget(url, schema)),
                "head",
            )
        assert UNSAFE_ROLE in _messages(caught.value)
        with admin.connect() as connection:
            attributes = connection.execute(
                text(
                    """
                    SELECT rolcanlogin, rolbypassrls, rolsuper,
                           rolcreatedb, rolcreaterole
                    FROM pg_roles
                    WHERE rolname = 'commitmentops_app'
                    """
                )
            ).one()
            tables = connection.scalar(
                text(
                    """
                    SELECT COUNT(*)
                    FROM information_schema.tables
                    WHERE table_schema = :schema
                    """
                ),
                {"schema": schema},
            )
        assert tuple(attributes) == (True, True, False, False, False)
        assert tables == 0
    finally:
        if created:
            drop_isolated_schema(admin, schema)
        _restore_application_role(admin)
        admin.dispose()


def test_bypass_runtime_login_is_not_granted() -> None:
    url = _postgres_url()
    admin = _admin_engine(url)
    schema = new_test_schema()
    created = False
    try:
        _ensure_role(
            admin,
            BYPASS_LOGIN,
            "LOGIN BYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE",
        )
        _create_schema(admin, schema)
        created = True
        with pytest.raises(Exception) as caught:
            command.upgrade(
                alembic_config(
                    IsolatedPostgresTarget(url, schema),
                    runtime_login=BYPASS_LOGIN,
                ),
                "head",
            )
        assert RUNTIME_LOGIN_REFUSAL in _messages(caught.value)
        with admin.connect() as connection:
            assert _is_app_member(connection, BYPASS_LOGIN) is False
            bypasses = connection.scalar(
                text(
                    """
                    SELECT rolbypassrls
                    FROM pg_roles
                    WHERE rolname = :login
                    """
                ),
                {"login": BYPASS_LOGIN},
            )
        assert bypasses is True
    finally:
        if created:
            drop_isolated_schema(admin, schema)
        _drop_managed_role(admin, BYPASS_LOGIN)
        admin.dispose()


def test_underprivileged_migration_login_changes_nothing() -> None:
    url = _postgres_url()
    admin = _admin_engine(url)
    schema = new_test_schema()
    created = False
    try:
        _ensure_role(
            admin,
            DENIED_LOGIN,
            "LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE",
        )
        _create_schema(admin, schema)
        created = True
        with admin.begin() as connection:
            connection.execute(
                text(
                    "GRANT USAGE, CREATE ON SCHEMA "
                    f"{quoted_isolated_schema(schema)} TO {DENIED_LOGIN}"
                )
            )
        with pytest.raises(Exception) as caught:
            command.upgrade(
                alembic_config(
                    IsolatedPostgresTarget(_role_url(url, DENIED_LOGIN), schema)
                ),
                "head",
            )
        assert "cannot administer roles" in _messages(caught.value)
        with admin.connect() as connection:
            tables = connection.scalar(
                text(
                    """
                    SELECT COUNT(*)
                    FROM information_schema.tables
                    WHERE table_schema = :schema
                    """
                ),
                {"schema": schema},
            )
        assert tables == 0
    finally:
        if created:
            drop_isolated_schema(admin, schema)
        _drop_managed_role(admin, DENIED_LOGIN)
        admin.dispose()


def test_table_owner_without_bypass_cannot_bootstrap_or_receive_the_app_role() -> None:
    url = _postgres_url()
    admin = _admin_engine(url)
    schema = new_test_schema()
    created = False
    try:
        _ensure_role(
            admin,
            OWNER_LOGIN,
            "LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT",
        )
        _create_schema(admin, schema)
        created = True
        target = IsolatedPostgresTarget(url, schema)
        command.upgrade(alembic_config(target), "20261016_03")
        with admin.begin() as connection:
            connection.execute(
                text(
                    f"ALTER TABLE {quoted_isolated_schema(schema)}.workflows "
                    f"OWNER TO {OWNER_LOGIN}"
                )
            )
        with pytest.raises(Exception) as caught:
            command.upgrade(
                alembic_config(target, runtime_login=OWNER_LOGIN),
                "head",
            )
        assert RUNTIME_LOGIN_REFUSAL in _messages(caught.value)
        with admin.connect() as connection:
            connection.execute(
                text(f"SET search_path TO {quoted_isolated_schema(schema)}")
            )
            version = connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert _is_app_member(connection, OWNER_LOGIN) is False
        assert version == "20261016_03"

        drop_isolated_schema(admin, schema)
        created = False
        with isolated_postgres_engine(url) as engine:
            migrated = require_test_target(engine)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE organizations OWNER TO "
                        f"{OWNER_LOGIN}"
                    )
                )
                connection.execute(
                    text(
                        "ALTER TABLE organization_memberships OWNER TO "
                        f"{OWNER_LOGIN}"
                    )
                )
                connection.execute(
                    text(
                        "GRANT USAGE ON SCHEMA "
                        f"{quoted_isolated_schema(migrated.schema)} TO {OWNER_LOGIN}"
                    )
                )
            with _dispose_after(
                create_database_engine(
                    _role_url(url, OWNER_LOGIN),
                    schema=migrated.schema,
                    connect_args={"connect_timeout": 5},
                )
            ) as owner_engine:
                with pytest.raises(
                    PersistenceError,
                    match="organization administration is unavailable",
                ):
                    OrganizationAdmin(create_session_factory(owner_engine)).save(
                        Organization(id=uuid4(), name="Owner bootstrap")
                    )
                OrganizationAdmin(create_session_factory(engine)).save(
                    Organization(id=uuid4(), name="Privileged bootstrap")
                )
    finally:
        if created:
            drop_isolated_schema(admin, schema)
        _drop_managed_role(admin, OWNER_LOGIN)
        admin.dispose()


def test_same_login_superuser_is_rejected_for_migration_and_requests(
    postgres_engine,
) -> None:
    url = _postgres_url()
    login = make_url(url).username
    if not login:
        raise AssertionError("migration login is missing")
    admin = _admin_engine(url)
    schema = new_test_schema()
    created = False
    try:
        _create_schema(admin, schema)
        created = True
        with pytest.raises(Exception) as caught:
            command.upgrade(
                alembic_config(
                    IsolatedPostgresTarget(url, schema),
                    runtime_login=login,
                ),
                "head",
            )
        assert RUNTIME_LOGIN_REFUSAL in _messages(caught.value)
        with admin.connect() as connection:
            is_superuser = connection.scalar(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = :login"),
                {"login": login},
            )
            tables = connection.scalar(
                text(
                    """
                    SELECT COUNT(*)
                    FROM information_schema.tables
                    WHERE table_schema = :schema
                    """
                ),
                {"schema": schema},
            )
        assert is_superuser is True
        assert tables == 0
    finally:
        if created:
            drop_isolated_schema(admin, schema)
        admin.dispose()

    with pytest.raises(PersistenceError, match=RUNTIME_LOGIN_REFUSAL):
        WorkflowRepository(create_session_factory(postgres_engine)).list(
            organization_id=uuid4(),
            user_id=uuid4(),
        )


def test_inherited_bypass_membership_is_rejected_without_changes() -> None:
    url = _postgres_url()
    admin = _admin_engine(url)
    schema = new_test_schema()
    created = False
    roles = (INHERITED_LOGIN, INHERIT_MID, INHERIT_BYPASS)
    try:
        _ensure_role(
            admin,
            INHERITED_LOGIN,
            "LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT",
        )
        _ensure_role(
            admin,
            INHERIT_MID,
            "NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE",
        )
        _ensure_role(
            admin,
            INHERIT_BYPASS,
            "NOLOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE",
        )
        with admin.begin() as connection:
            connection.execute(text(f"GRANT {INHERIT_BYPASS} TO {INHERIT_MID}"))
            connection.execute(text(f"GRANT {INHERIT_MID} TO {INHERITED_LOGIN}"))
        _create_schema(admin, schema)
        created = True
        with pytest.raises(Exception) as caught:
            command.upgrade(
                alembic_config(
                    IsolatedPostgresTarget(url, schema),
                    runtime_login=INHERITED_LOGIN,
                ),
                "head",
            )
        assert RUNTIME_LOGIN_REFUSAL in _messages(caught.value)
        with admin.connect() as connection:
            bypasses = connection.scalar(
                text(
                    "SELECT rolbypassrls FROM pg_roles WHERE rolname = :role"
                ),
                {"role": INHERIT_BYPASS},
            )
            assert _has_direct_membership(
                connection, INHERIT_MID, INHERIT_BYPASS
            )
            assert _has_direct_membership(
                connection, INHERITED_LOGIN, INHERIT_MID
            )
        assert bypasses is True
    finally:
        if created:
            drop_isolated_schema(admin, schema)
        _release_roles(admin, roles)
        admin.dispose()


def test_inherited_owner_membership_blocks_protected_requests() -> None:
    url = _postgres_url()
    admin = _admin_engine(url)
    roles = (OWNER_MEMBER, OWNER_MID, OWNER_GROUP)
    try:
        _ensure_role(
            admin,
            OWNER_MEMBER,
            "LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT",
        )
        _ensure_role(
            admin,
            OWNER_MID,
            "NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE",
        )
        _ensure_role(
            admin,
            OWNER_GROUP,
            "NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE",
        )
        with isolated_postgres_engine(url, runtime_login=OWNER_MEMBER) as engine:
            target = require_test_target(engine)
            with engine.begin() as connection:
                connection.execute(
                    text(f"ALTER TABLE workflows OWNER TO {OWNER_GROUP}")
                )
                connection.execute(text(f"GRANT {OWNER_GROUP} TO {OWNER_MID}"))
                connection.execute(text(f"GRANT {OWNER_MID} TO {OWNER_MEMBER}"))
                connection.execute(
                    text(
                        "GRANT USAGE ON SCHEMA "
                        f"{quoted_isolated_schema(target.schema)} TO {OWNER_MEMBER}"
                    )
                )
            with _dispose_after(
                create_database_engine(
                    _role_url(url, OWNER_MEMBER),
                    schema=target.schema,
                    connect_args={"connect_timeout": 5},
                )
            ) as request_engine:
                with pytest.raises(PersistenceError, match=RUNTIME_LOGIN_REFUSAL):
                    WorkflowRepository(create_session_factory(request_engine)).list(
                        organization_id=uuid4(),
                        user_id=uuid4(),
                    )
            with engine.connect() as connection:
                owns_workflows = connection.scalar(
                    text(
                        """
                        SELECT pg_get_userbyid(pg_class.relowner) = :owner
                        FROM pg_class
                        JOIN pg_namespace
                          ON pg_namespace.oid = pg_class.relnamespace
                        WHERE pg_namespace.nspname = current_schema()
                          AND pg_class.relname = 'workflows'
                        """
                    ),
                    {"owner": OWNER_GROUP},
                )
                assert owns_workflows is True
                assert _has_direct_membership(connection, OWNER_MID, OWNER_GROUP)
                assert _has_direct_membership(connection, OWNER_MEMBER, OWNER_MID)
    finally:
        _release_roles(admin, roles)
        admin.dispose()


def _assume_member(connection, user_id: UUID, organization_id: UUID) -> None:
    connection.execute(text("SET LOCAL ROLE commitmentops_app"))
    connection.execute(
        text("SELECT set_config('app.user_id', :user_id, true)"),
        {"user_id": str(user_id)},
    )
    connection.execute(
        text("SELECT set_config('app.organization_id', :organization_id, true)"),
        {"organization_id": str(organization_id)},
    )


def _ids(connection, table_name: str) -> list[UUID]:
    if table_name not in {
        "workflows",
        "requirements",
        "evidence",
        "organizations",
        "people",
    }:
        raise AssertionError("unexpected table")
    return list(connection.scalars(text(f"SELECT id FROM {table_name}")).all())


def _seed(engine) -> dict[str, UUID]:
    org_a, org_b = uuid4(), uuid4()
    user_a, user_b = uuid4(), uuid4()
    workflow_a, workflow_b = uuid4(), uuid4()
    requirement_a, requirement_b = uuid4(), uuid4()
    evidence_a, evidence_b = uuid4(), uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO organizations (id, name) VALUES (:id, :name)"),
            [
                {"id": org_a, "name": "Organization A"},
                {"id": org_b, "name": "Organization B"},
            ],
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
                {"organization_id": org_a, "user_id": user_a},
                {"organization_id": org_b, "user_id": user_b},
            ],
        )
        connection.execute(
            text(
                "INSERT INTO people (id, name, email) VALUES (:id, :name, :email)"
            ),
            [
                {"id": user_a, "name": "Member A", "email": "a@example.test"},
                {"id": user_b, "name": "Member B", "email": "b@example.test"},
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO workflows (
                    id, organization_id, name, workflow_type, status
                )
                VALUES (:id, :organization_id, :name, 'general', 'draft')
                """
            ),
            [
                {
                    "id": workflow_a,
                    "organization_id": org_a,
                    "name": "Organization A",
                },
                {
                    "id": workflow_b,
                    "organization_id": org_b,
                    "name": "Organization B",
                },
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO requirements (
                    id, workflow_id, title, description, owner_id,
                    evidence_expectation, provenance, status, review_status
                )
                VALUES (
                    :id, :workflow_id, 'Requirement', 'Scoped requirement',
                    :owner_id, 'A record', 'user_provided', 'not_started',
                    'pending_review'
                )
                """
            ),
            [
                {
                    "id": requirement_a,
                    "workflow_id": workflow_a,
                    "owner_id": user_a,
                },
                {
                    "id": requirement_b,
                    "workflow_id": workflow_b,
                    "owner_id": user_b,
                },
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO evidence (
                    id, requirement_id, description, evidence_type,
                    submitted_by_id, submitted_at, status
                )
                VALUES (
                    :id, :requirement_id, 'Scoped evidence', 'note',
                    :submitted_by_id, :submitted_at, 'submitted'
                )
                """
            ),
            [
                {
                    "id": evidence_a,
                    "requirement_id": requirement_a,
                    "submitted_by_id": user_a,
                    "submitted_at": now,
                },
                {
                    "id": evidence_b,
                    "requirement_id": requirement_b,
                    "submitted_by_id": user_b,
                    "submitted_at": now,
                },
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, occurred_at, entity_type, entity_id, event_type, after_state
                )
                VALUES (
                    :id, :occurred_at, 'workflow', :entity_id,
                    'workflow.created', CAST(:after_state AS JSON)
                )
                """
            ),
            [
                {
                    "id": uuid4(),
                    "occurred_at": now,
                    "entity_id": workflow_a,
                    "after_state": '{"status":"draft"}',
                },
                {
                    "id": uuid4(),
                    "occurred_at": now,
                    "entity_id": workflow_b,
                    "after_state": '{"status":"draft"}',
                },
            ],
        )
    return {
        "org_a": org_a,
        "org_b": org_b,
        "user_a": user_a,
        "user_b": user_b,
        "workflow_a": workflow_a,
        "workflow_b": workflow_b,
        "requirement_a": requirement_a,
        "requirement_b": requirement_b,
        "evidence_a": evidence_a,
        "evidence_b": evidence_b,
    }


RUNTIME_LOGIN = "commitmentops_runtime"
BYPASS_LOGIN = "commitmentops_runtime_bypass"
OWNER_LOGIN = "commitmentops_table_owner"
DENIED_LOGIN = "commitmentops_migrate_denied"
MEMBER_LOGIN = "commitmentops_member"
INHERITED_LOGIN = "commitmentops_inherited"
INHERIT_MID = "commitmentops_inherit_mid"
INHERIT_BYPASS = "commitmentops_inherit_bypass"
OWNER_MEMBER = "commitmentops_owner_member"
OWNER_MID = "commitmentops_owner_mid"
OWNER_GROUP = "commitmentops_owner_group"
_MANAGED_ROLES = frozenset(
    {
        RUNTIME_LOGIN,
        BYPASS_LOGIN,
        OWNER_LOGIN,
        DENIED_LOGIN,
        MEMBER_LOGIN,
        INHERITED_LOGIN,
        INHERIT_MID,
        INHERIT_BYPASS,
        OWNER_MEMBER,
        OWNER_MID,
        OWNER_GROUP,
    }
)
UNSAFE_ROLE = (
    "Refusing to use existing role commitmentops_app because it is not "
    "NOLOGIN, NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, and NOCREATEROLE"
)


@contextmanager
def _dispose_after(engine) -> Iterator[object]:
    try:
        yield engine
    finally:
        engine.dispose()


def _postgres_url() -> str:
    configured = explicit_test_database_url()
    if configured is None:
        pytest.skip(SKIP_REASON)
    return assert_postgres_test_url(configured)


def _admin_engine(url: str):
    return create_engine(
        url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"connect_timeout": 5},
    )


def _role_url(url: str, role: str) -> str:
    if role not in _MANAGED_ROLES:
        raise AssertionError("unexpected role")
    parsed = make_url(url)
    return URL.create(
        drivername=parsed.drivername,
        username=role,
        password=None,
        host=parsed.host,
        port=parsed.port,
        database=parsed.database,
    ).render_as_string(hide_password=False)


def _messages(exc: BaseException) -> str:
    parts: list[str] = []
    pending: list[BaseException | None] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        parts.append(str(current))
        pending.append(current.__cause__)
        pending.append(current.__context__)
    return "\n".join(parts)


def _is_app_member(connection, role: str) -> bool:
    return bool(
        connection.scalar(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_auth_members
                    JOIN pg_roles AS member_role
                      ON member_role.oid = pg_auth_members.member
                    JOIN pg_roles AS granted
                      ON granted.oid = pg_auth_members.roleid
                    WHERE member_role.rolname = :member
                      AND granted.rolname = :role
                )
                """
            ),
            {"member": role, "role": APP_ROLE},
        )
    )


@contextmanager
def _request_connection(admin_engine):
    target = require_test_target(admin_engine)
    quoted = quoted_isolated_schema(target.schema)
    _ensure_role(
        admin_engine,
        MEMBER_LOGIN,
        "LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT",
    )
    with admin_engine.begin() as connection:
        connection.execute(text(f"GRANT {APP_ROLE} TO {MEMBER_LOGIN}"))
        connection.execute(
            text(f"GRANT USAGE ON SCHEMA {quoted} TO {MEMBER_LOGIN}")
        )
    engine = create_database_engine(
        _role_url(_postgres_url(), MEMBER_LOGIN),
        schema=target.schema,
        connect_args={"connect_timeout": 5},
    )
    try:
        yield engine
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                text(f"REVOKE ALL ON SCHEMA {quoted} FROM {MEMBER_LOGIN}")
            )
        _drop_managed_role(admin_engine, MEMBER_LOGIN)


def _has_direct_membership(connection, member: str, granted: str) -> bool:
    if member not in _MANAGED_ROLES or granted not in _MANAGED_ROLES:
        raise AssertionError("unexpected role")
    return bool(
        connection.scalar(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_auth_members
                    JOIN pg_roles AS member_role
                      ON member_role.oid = pg_auth_members.member
                    JOIN pg_roles AS granted_role
                      ON granted_role.oid = pg_auth_members.roleid
                    WHERE member_role.rolname = :member
                      AND granted_role.rolname = :granted
                )
                """
            ),
            {"member": member, "granted": granted},
        )
    )


def _release_roles(admin, names: tuple[str, ...]) -> None:
    with admin.begin() as connection:
        for granted in names:
            for member in names:
                if granted != member and _has_direct_membership(
                    connection, member, granted
                ):
                    connection.execute(text(f"REVOKE {granted} FROM {member}"))
    for name in names:
        _drop_managed_role(admin, name)


def _ensure_role(admin, name: str, attributes: str) -> None:
    if name not in _MANAGED_ROLES:
        raise AssertionError("unexpected role")
    _drop_managed_role(admin, name)
    with admin.begin() as connection:
        connection.execute(text(f"CREATE ROLE {name} {attributes}"))


def _drop_managed_role(admin, name: str) -> None:
    if name not in _MANAGED_ROLES:
        raise AssertionError("unexpected role")
    with admin.begin() as connection:
        if _is_app_member(connection, name):
            connection.execute(text(f"REVOKE {APP_ROLE} FROM {name}"))
        exists = connection.scalar(
            text("SELECT 1 FROM pg_roles WHERE rolname = :name"),
            {"name": name},
        )
        if exists:
            connection.execute(text(f"DROP ROLE {name}"))


def _swap_in_unsafe_application_role(admin) -> None:
    with admin.begin() as connection:
        held = connection.scalar(
            text(
                "SELECT 1 FROM pg_roles WHERE rolname = 'commitmentops_app_held'"
            )
        )
        if held:
            raise AssertionError("leftover held application role")
        exists = connection.scalar(
            text("SELECT 1 FROM pg_roles WHERE rolname = 'commitmentops_app'")
        )
        if exists:
            connection.execute(
                text(
                    "ALTER ROLE commitmentops_app RENAME TO commitmentops_app_held"
                )
            )
        connection.execute(
            text(
                "CREATE ROLE commitmentops_app "
                "LOGIN BYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE"
            )
        )


def _restore_application_role(admin) -> None:
    with admin.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT rolcanlogin, rolbypassrls
                FROM pg_roles
                WHERE rolname = 'commitmentops_app'
                """
            )
        ).one_or_none()
        if row is not None and row.rolcanlogin and row.rolbypassrls:
            connection.execute(text("DROP ROLE commitmentops_app"))
        held = connection.scalar(
            text(
                "SELECT 1 FROM pg_roles WHERE rolname = 'commitmentops_app_held'"
            )
        )
        if held:
            connection.execute(
                text(
                    "ALTER ROLE commitmentops_app_held RENAME TO commitmentops_app"
                )
            )
