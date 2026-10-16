from uuid import UUID, uuid4

from alembic import command
import pytest
from sqlalchemy import text

from app.persistence import (
    Organization,
    OrganizationRepository,
    PersistenceError,
    WorkflowRepository,
    create_session_factory,
)
from tests.persistence.postgres_support import (
    SKIP_REASON,
    alembic_config,
    clear_isolated_tables,
    explicit_test_database_url,
    isolated_postgres_engine,
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


@pytest.fixture
def postgres_repository(postgres_engine) -> WorkflowRepository:
    return WorkflowRepository(create_session_factory(postgres_engine))


@pytest.fixture
def postgres_organizations(postgres_engine) -> OrganizationRepository:
    return OrganizationRepository(create_session_factory(postgres_engine))


def test_connection_stays_on_the_isolated_schema(postgres_engine) -> None:
    target = require_test_target(postgres_engine)
    with postgres_engine.connect() as connection:
        assert connection.scalar(text("SELECT current_schema()")) == target.schema
    assert target.schema != "public"
    assert target.schema.startswith("commitmentops_test_")


def test_organization_membership_round_trip(
    postgres_organizations: OrganizationRepository,
) -> None:
    organization_id = uuid4()
    user_id = uuid4()
    postgres_organizations.save(
        Organization(id=organization_id, name="Synthetic PostgreSQL Organization")
    )
    postgres_organizations.set_membership(
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
    postgres_organizations.set_membership(
        organization_id=organization_id,
        user_id=user_id,
        is_active=False,
    )
    assert not postgres_organizations.has_active_membership(
        organization_id=organization_id,
        user_id=user_id,
    )


def test_aggregate_save_and_reload(
    postgres_organizations: OrganizationRepository,
    postgres_repository: WorkflowRepository,
    persisted_workflow,
) -> None:
    assert persisted_workflow.organization_id is not None
    postgres_organizations.save(
        Organization(
            id=persisted_workflow.organization_id,
            name="Synthetic Test Organization",
        )
    )
    postgres_repository.save(
        persisted_workflow,
        actor_reference=str(uuid4()),
        correlation_id="synthetic-intake-001",
    )

    loaded = postgres_repository.get(
        persisted_workflow.id,
        organization_id=persisted_workflow.organization_id,
    )
    events = postgres_repository.list_audit_events(
        persisted_workflow.id,
        organization_id=persisted_workflow.organization_id,
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
    postgres_organizations: OrganizationRepository,
    postgres_repository: WorkflowRepository,
    persisted_workflow,
) -> None:
    assert persisted_workflow.organization_id is not None
    postgres_organizations.save(
        Organization(
            id=persisted_workflow.organization_id,
            name="Synthetic Test Organization",
        )
    )
    postgres_repository.save(persisted_workflow)
    other_organization_id = UUID("10000000-0000-0000-0000-000000000002")

    assert (
        postgres_repository.get(
            persisted_workflow.id,
            organization_id=other_organization_id,
        )
        is None
    )
    assert postgres_repository.list(organization_id=other_organization_id) == []
    assert (
        postgres_repository.list_audit_events(
            persisted_workflow.id,
            organization_id=other_organization_id,
        )
        == []
    )
    assert (
        len(
            postgres_repository.list(
                organization_id=persisted_workflow.organization_id
            )
        )
        == 1
    )


def test_constraint_failure_rolls_back_the_transaction(
    postgres_organizations: OrganizationRepository,
    postgres_repository: WorkflowRepository,
    persisted_workflow,
) -> None:
    assert persisted_workflow.organization_id is not None
    postgres_organizations.save(
        Organization(
            id=persisted_workflow.organization_id,
            name="Synthetic Test Organization",
        )
    )

    with pytest.raises(PersistenceError, match="persistence failed"):
        postgres_repository.save(persisted_workflow, actor_reference="x" * 256)

    assert (
        postgres_repository.get(
            persisted_workflow.id,
            organization_id=persisted_workflow.organization_id,
        )
        is None
    )
    assert (
        postgres_repository.list_audit_events(
            persisted_workflow.id,
            organization_id=persisted_workflow.organization_id,
        )
        == []
    )


def test_downgrade_is_refused_when_memberships_exist(
    postgres_engine,
    postgres_organizations: OrganizationRepository,
) -> None:
    organization_id = uuid4()
    postgres_organizations.save(
        Organization(id=organization_id, name="Synthetic PostgreSQL Organization")
    )
    postgres_organizations.set_membership(
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
    postgres_organizations: OrganizationRepository,
) -> None:
    organization_id = uuid4()
    postgres_organizations.save(
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
