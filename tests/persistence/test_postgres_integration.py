from uuid import UUID, uuid4

from alembic import command
import pytest
from sqlalchemy import text

from app.persistence import (
    Organization,
    OrganizationAdmin,
    OrganizationRepository,
    PersistenceError,
    WorkflowRepository,
    create_database_engine,
    create_session_factory,
)
from app.persistence.database import quoted_isolated_schema
from app.persistence.rls import APP_ROLE
from tests.persistence.postgres_support import (
    SKIP_REASON,
    alembic_config,
    clear_isolated_tables,
    assign_test_role_password,
    create_test_login,
    explicit_test_database_url,
    new_test_password,
    isolated_postgres_engine,
    require_test_target,
    temporary_role_url,
)

pytestmark = pytest.mark.postgres

MEMBER_ID = UUID("20000000-0000-0000-0000-000000000009")
REQUEST_LOGIN = "commitmentops_request"
_REQUEST_PASSWORD: str | None = None


def _authorize(admin: OrganizationAdmin, organization_id: UUID) -> None:
    admin.save(
        Organization(id=organization_id, name="Synthetic Test Organization")
    )
    admin.set_membership(
        organization_id=organization_id,
        user_id=MEMBER_ID,
        is_active=True,
    )


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


@pytest.fixture(scope="module")
def postgres_request_engine(postgres_engine):
    """Restricted login for protected repository calls.

    The migration fixture stays on the privileged harness connection.
    """

    url = explicit_test_database_url()
    if url is None:
        pytest.skip(SKIP_REASON)
    target = require_test_target(postgres_engine)
    _ensure_request_login(postgres_engine, target.schema)
    engine = create_database_engine(
        _request_login_url(url),
        schema=target.schema,
        connect_args={"connect_timeout": 5},
    )
    try:
        yield engine
    finally:
        engine.dispose()
        _drop_request_login(postgres_engine, target.schema)


@pytest.fixture
def postgres_repository(postgres_request_engine) -> WorkflowRepository:
    return WorkflowRepository(create_session_factory(postgres_request_engine))


@pytest.fixture
def postgres_admin(postgres_engine) -> OrganizationAdmin:
    return OrganizationAdmin(create_session_factory(postgres_engine))


@pytest.fixture
def postgres_organizations(postgres_request_engine) -> OrganizationRepository:
    return OrganizationRepository(create_session_factory(postgres_request_engine))


def test_connection_stays_on_the_isolated_schema(postgres_engine) -> None:
    target = require_test_target(postgres_engine)
    with postgres_engine.connect() as connection:
        assert connection.scalar(text("SELECT current_schema()")) == target.schema
    assert target.schema != "public"
    assert target.schema.startswith("commitmentops_test_")


def test_organization_membership_round_trip(
    postgres_admin: OrganizationAdmin,
    postgres_organizations: OrganizationRepository,
) -> None:
    organization_id = uuid4()
    user_id = uuid4()
    postgres_admin.save(
        Organization(id=organization_id, name="Synthetic PostgreSQL Organization")
    )
    postgres_admin.set_membership(
        organization_id=organization_id,
        user_id=user_id,
        is_active=True,
    )

    assert postgres_organizations.has_active_membership(
        organization_id=organization_id,
        user_id=user_id,
    )
    assert not postgres_organizations.has_active_membership(
        organization_id=organization_id,
        user_id=uuid4(),
    )
    postgres_admin.set_membership(
        organization_id=organization_id,
        user_id=user_id,
        is_active=False,
    )
    assert not postgres_organizations.has_active_membership(
        organization_id=organization_id,
        user_id=user_id,
    )


def test_aggregate_save_and_reload(
    postgres_admin: OrganizationAdmin,
    postgres_repository: WorkflowRepository,
    persisted_workflow,
) -> None:
    assert persisted_workflow.organization_id is not None
    _authorize(postgres_admin, persisted_workflow.organization_id)
    postgres_repository.save(
        persisted_workflow,
        user_id=MEMBER_ID,
        organization_id=persisted_workflow.organization_id,
        actor_reference=str(uuid4()),
        correlation_id="synthetic-intake-001",
    )

    loaded = postgres_repository.get(
        persisted_workflow.id,
        organization_id=persisted_workflow.organization_id,
        user_id=MEMBER_ID,
    )
    events = postgres_repository.list_audit_events(
        persisted_workflow.id,
        organization_id=persisted_workflow.organization_id,
        user_id=MEMBER_ID,
    )

    assert loaded is not None
    assert loaded.model_dump() == persisted_workflow.model_dump()
    requirement = loaded.requirements[0]
    assert requirement.evidence[0].description == "Q3 participant register"
    assert len(events) == 1
    assert events[0].event_type == "workflow.created"
    assert events[0].after_state["status"] == persisted_workflow.status
    assert events[0].correlation_id == "synthetic-intake-001"


def test_reads_and_lists_stay_inside_the_organization(
    postgres_admin: OrganizationAdmin,
    postgres_repository: WorkflowRepository,
    persisted_workflow,
) -> None:
    assert persisted_workflow.organization_id is not None
    _authorize(postgres_admin, persisted_workflow.organization_id)
    postgres_repository.save(
        persisted_workflow,
        user_id=MEMBER_ID,
        organization_id=persisted_workflow.organization_id,
    )
    other_organization_id = UUID("10000000-0000-0000-0000-000000000002")

    assert (
        postgres_repository.get(
            persisted_workflow.id,
            organization_id=other_organization_id,
            user_id=MEMBER_ID,
        )
        is None
    )
    assert (
        postgres_repository.list(
            organization_id=other_organization_id,
            user_id=MEMBER_ID,
        )
        == []
    )
    assert (
        postgres_repository.list_audit_events(
            persisted_workflow.id,
            organization_id=other_organization_id,
            user_id=MEMBER_ID,
        )
        == []
    )
    assert (
        len(
            postgres_repository.list(
                organization_id=persisted_workflow.organization_id,
                user_id=MEMBER_ID,
            )
        )
        == 1
    )


def test_constraint_failure_rolls_back_the_transaction(
    postgres_admin: OrganizationAdmin,
    postgres_repository: WorkflowRepository,
    persisted_workflow,
) -> None:
    assert persisted_workflow.organization_id is not None
    _authorize(postgres_admin, persisted_workflow.organization_id)

    with pytest.raises(PersistenceError, match="persistence failed"):
        postgres_repository.save(
            persisted_workflow,
            user_id=MEMBER_ID,
            organization_id=persisted_workflow.organization_id,
            actor_reference="x" * 256,
        )

    assert (
        postgres_repository.get(
            persisted_workflow.id,
            organization_id=persisted_workflow.organization_id,
            user_id=MEMBER_ID,
        )
        is None
    )
    assert (
        postgres_repository.list_audit_events(
            persisted_workflow.id,
            organization_id=persisted_workflow.organization_id,
            user_id=MEMBER_ID,
        )
        == []
    )


def test_downgrade_is_refused_when_memberships_exist(
    postgres_engine,
    postgres_admin: OrganizationAdmin,
) -> None:
    organization_id = uuid4()
    postgres_admin.save(
        Organization(id=organization_id, name="Synthetic PostgreSQL Organization")
    )
    postgres_admin.set_membership(
        organization_id=organization_id,
        user_id=uuid4(),
        is_active=True,
    )
    target = require_test_target(postgres_engine)

    with pytest.raises(RuntimeError, match="Refusing to downgrade"):
        command.downgrade(alembic_config(target), "20261009_01")

    with postgres_engine.connect() as connection:
        assert connection.scalar(text("SELECT current_schema()")) == target.schema
        still_present = connection.scalar(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = :schema AND table_name = 'organizations'"
            ),
            {"schema": target.schema},
        )
    assert still_present == 1


def test_downgrade_is_refused_for_an_organization_without_children(
    postgres_engine,
    postgres_admin: OrganizationAdmin,
) -> None:
    organization_id = uuid4()
    postgres_admin.save(
        Organization(id=organization_id, name="Synthetic Empty PostgreSQL Organization")
    )
    target = require_test_target(postgres_engine)

    with pytest.raises(RuntimeError, match="Refusing to downgrade"):
        command.downgrade(alembic_config(target), "20261009_01")

    with postgres_engine.connect() as connection:
        assert connection.scalar(text("SELECT current_schema()")) == target.schema
        stored_name = connection.scalar(
            text(
                "SELECT name FROM organizations "
                "WHERE id = :organization_id"
            ),
            {"organization_id": organization_id},
        )
    assert stored_name == "Synthetic Empty PostgreSQL Organization"


def _request_login_url(url: str) -> str:
    if _REQUEST_PASSWORD is None:
        raise AssertionError("test login has no password")
    return temporary_role_url(url, REQUEST_LOGIN, _REQUEST_PASSWORD)


def _ensure_request_login(engine, schema: str) -> None:
    global _REQUEST_PASSWORD
    quoted = quoted_isolated_schema(schema)
    with engine.begin() as connection:
        existing = connection.execute(
            text(
                """
                SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolcanlogin
                FROM pg_roles
                WHERE rolname = :role
                """
            ),
            {"role": REQUEST_LOGIN},
        ).one_or_none()
        if existing is None:
            _REQUEST_PASSWORD = create_test_login(
                connection,
                REQUEST_LOGIN,
                "LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT",
            )
        elif tuple(existing) != (False, False, False, False, True):
            raise AssertionError("request login has unexpected privileges")
        else:
            _REQUEST_PASSWORD = new_test_password()
            assign_test_role_password(
                connection, REQUEST_LOGIN, _REQUEST_PASSWORD
            )
        connection.execute(text(f"GRANT {APP_ROLE} TO {REQUEST_LOGIN}"))
        connection.execute(text(f"GRANT USAGE ON SCHEMA {quoted} TO {REQUEST_LOGIN}"))


def _drop_request_login(engine, schema: str) -> None:
    global _REQUEST_PASSWORD
    quoted = quoted_isolated_schema(schema)
    with engine.begin() as connection:
        exists = connection.scalar(
            text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
            {"role": REQUEST_LOGIN},
        )
        if not exists:
            _REQUEST_PASSWORD = None
            return
        member = connection.scalar(
            text("SELECT pg_has_role(:role, :app_role, 'MEMBER')"),
            {"role": REQUEST_LOGIN, "app_role": APP_ROLE},
        )
        if member:
            connection.execute(text(f"REVOKE {APP_ROLE} FROM {REQUEST_LOGIN}"))
        connection.execute(text(f"REVOKE ALL ON SCHEMA {quoted} FROM {REQUEST_LOGIN}"))
        connection.execute(text(f"DROP ROLE {REQUEST_LOGIN}"))
        _REQUEST_PASSWORD = None
