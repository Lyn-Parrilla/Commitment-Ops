from pathlib import Path
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa
from sqlalchemy import inspect

from app.config import get_settings
from app.persistence import create_database_engine


def test_docker_build_context_packages_migration_runtime_files() -> None:
    repository_root = Path(__file__).parents[2]
    dockerfile = (repository_root / "Dockerfile").read_text()
    dockerignore = [
        line.strip()
        for line in (repository_root / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert "WORKDIR /app" in dockerfile
    assert "COPY alembic.ini ./" in dockerfile
    assert "COPY migrations ./migrations" in dockerfile
    assert "alembic.ini" not in dockerignore
    assert "migrations" not in dockerignore
    assert not any(
        _dockerignore_excludes(pattern, "alembic.ini")
        for pattern in dockerignore
    )
    assert not any(
        _dockerignore_excludes(pattern, "migrations")
        for pattern in dockerignore
    )
    assert (repository_root / "migrations" / "env.py").is_file()
    assert (repository_root / "migrations" / "script.py.mako").is_file()
    assert any((repository_root / "migrations" / "versions").glob("*.py"))


def test_initial_migration_creates_accountability_schema(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'migration-test.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()

    config = Config("alembic.ini")
    command.upgrade(config, "head")
    command.check(config)

    engine = create_database_engine(database_url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    get_settings.cache_clear()

    assert {
        "alembic_version",
        "audit_events",
        "evidence",
        "organization_memberships",
        "organizations",
        "people",
        "requirements",
        "workflows",
    } <= tables


def test_tenant_migration_preserves_existing_workflows_as_unassigned(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'legacy-migration.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    command.upgrade(config, "20261009_01")

    workflow_id = uuid4()
    legacy_workflows = sa.table(
        "workflows",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("workflow_type", sa.String()),
        sa.column("status", sa.String()),
    )
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.insert(legacy_workflows).values(
                id=workflow_id,
                name="Legacy Synthetic Workflow",
                workflow_type="general",
                status="draft",
            )
        )
    engine.dispose()

    command.upgrade(config, "head")

    migrated_workflows = sa.table(
        "workflows",
        sa.column("id", sa.Uuid()),
        sa.column("organization_id", sa.Uuid()),
    )
    memberships = sa.table(
        "organization_memberships",
        sa.column("organization_id", sa.Uuid()),
    )
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        organization_id = connection.scalar(
            sa.select(migrated_workflows.c.organization_id).where(
                migrated_workflows.c.id == workflow_id
            )
        )
        membership_count = connection.scalar(
            sa.select(sa.func.count()).select_from(memberships)
        )
    columns = {
        column["name"]: column for column in inspect(engine).get_columns("workflows")
    }
    engine.dispose()
    get_settings.cache_clear()

    assert organization_id == UUID("00000000-0000-0000-0000-000000000001")
    assert membership_count == 0
    assert columns["organization_id"]["nullable"] is False


def test_downgrade_refuses_non_legacy_tenant_assignments(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'tenant-downgrade.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    command.upgrade(config, "head")

    organization_id = uuid4()
    workflow_id = uuid4()
    organizations = sa.table(
        "organizations",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
    )
    memberships = sa.table(
        "organization_memberships",
        sa.column("organization_id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
        sa.column("is_active", sa.Boolean()),
    )
    workflows = sa.table(
        "workflows",
        sa.column("id", sa.Uuid()),
        sa.column("organization_id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("workflow_type", sa.String()),
        sa.column("status", sa.String()),
    )
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.insert(organizations).values(
                id=organization_id,
                name="Synthetic Tenant",
            )
        )
        connection.execute(
            sa.insert(memberships).values(
                organization_id=organization_id,
                user_id=uuid4(),
                is_active=True,
            )
        )
        connection.execute(
            sa.insert(workflows).values(
                id=workflow_id,
                organization_id=organization_id,
                name="Assigned Synthetic Workflow",
                workflow_type="general",
                status="draft",
            )
        )
    engine.dispose()

    with pytest.raises(Exception, match="Refusing to downgrade"):
        command.downgrade(config, "20261009_01")
    command.check(config)

    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        stored_organization_id = connection.scalar(
            sa.select(workflows.c.organization_id).where(
                workflows.c.id == workflow_id
            )
        )
    engine.dispose()
    get_settings.cache_clear()

    assert stored_organization_id == organization_id


def test_downgrade_refuses_nonlegacy_organization_without_children(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'empty-tenant-downgrade.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    command.upgrade(config, "head")

    organization_id = uuid4()
    organizations = sa.table(
        "organizations",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
    )
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.insert(organizations).values(
                id=organization_id,
                name="Synthetic Empty Tenant",
            )
        )
    engine.dispose()

    with pytest.raises(Exception, match="Refusing to downgrade"):
        command.downgrade(config, "20261009_01")
    command.check(config)

    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        stored_name = connection.scalar(
            sa.select(organizations.c.name).where(
                organizations.c.id == organization_id
            )
        )
        tables = set(inspect(engine).get_table_names())
    engine.dispose()
    get_settings.cache_clear()

    assert stored_name == "Synthetic Empty Tenant"
    assert "organizations" in tables
    assert "organization_memberships" in tables


def test_renamed_placeholder_does_not_hide_a_nonlegacy_organization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'renamed-placeholder.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    command.upgrade(config, "head")

    legacy_organization_id = UUID("00000000-0000-0000-0000-000000000001")
    other_organization_id = uuid4()
    organizations = sa.table(
        "organizations",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
    )
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.update(organizations)
            .where(organizations.c.id == legacy_organization_id)
            .values(name="Renamed Placeholder")
        )
        connection.execute(
            sa.insert(organizations).values(
                id=other_organization_id,
                name="Synthetic Empty Tenant",
            )
        )
    engine.dispose()

    with pytest.raises(Exception, match="Refusing to downgrade"):
        command.downgrade(config, "20261009_01")

    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        renamed = connection.scalar(
            sa.select(organizations.c.name).where(
                organizations.c.id == legacy_organization_id
            )
        )
        other = connection.scalar(
            sa.select(organizations.c.name).where(
                organizations.c.id == other_organization_id
            )
        )
    engine.dispose()
    get_settings.cache_clear()

    assert renamed == "Renamed Placeholder"
    assert other == "Synthetic Empty Tenant"


def test_downgrade_refuses_an_inactive_membership_on_the_placeholder(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'legacy-membership.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    command.upgrade(config, "head")

    legacy_organization_id = UUID("00000000-0000-0000-0000-000000000001")
    user_id = uuid4()
    memberships = sa.table(
        "organization_memberships",
        sa.column("organization_id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
        sa.column("is_active", sa.Boolean()),
    )
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.insert(memberships).values(
                organization_id=legacy_organization_id,
                user_id=user_id,
                is_active=False,
            )
        )
    engine.dispose()

    with pytest.raises(Exception, match="Refusing to downgrade"):
        command.downgrade(config, "20261009_01")

    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        stored_user = connection.scalar(
            sa.select(memberships.c.user_id).where(
                memberships.c.organization_id == legacy_organization_id
            )
        )
        tables = set(inspect(engine).get_table_names())
    engine.dispose()
    get_settings.cache_clear()

    assert stored_user == user_id
    assert "organizations" in tables
    assert "organization_memberships" in tables


def test_renamed_placeholder_name_is_dropped_with_the_organization_table(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'renamed-only.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    command.upgrade(config, "20261009_01")

    workflow_id = uuid4()
    legacy_workflows = sa.table(
        "workflows",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("workflow_type", sa.String()),
        sa.column("status", sa.String()),
    )
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.insert(legacy_workflows).values(
                id=workflow_id,
                name="Legacy Synthetic Workflow",
                workflow_type="general",
                status="draft",
            )
        )
    engine.dispose()
    command.upgrade(config, "head")

    legacy_organization_id = UUID("00000000-0000-0000-0000-000000000001")
    organizations = sa.table(
        "organizations",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
    )
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.update(organizations)
            .where(organizations.c.id == legacy_organization_id)
            .values(name="Renamed Placeholder")
        )
    engine.dispose()
    command.downgrade(config, "20261009_01")

    stored = sa.table(
        "workflows",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
    )
    engine = create_database_engine(database_url)
    tables = set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        name = connection.scalar(
            sa.select(stored.c.name).where(stored.c.id == workflow_id)
        )
    engine.dispose()
    get_settings.cache_clear()

    assert name == "Legacy Synthetic Workflow"
    assert "organizations" not in tables
    assert "organization_memberships" not in tables


def test_downgrade_removes_only_the_legacy_placeholder(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'legacy-downgrade.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    command.upgrade(config, "20261009_01")

    workflow_id = uuid4()
    legacy_workflows = sa.table(
        "workflows",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("workflow_type", sa.String()),
        sa.column("status", sa.String()),
    )
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.insert(legacy_workflows).values(
                id=workflow_id,
                name="Legacy Synthetic Workflow",
                workflow_type="general",
                status="draft",
            )
        )
    engine.dispose()
    command.upgrade(config, "head")
    command.downgrade(config, "20261009_01")

    engine = create_database_engine(database_url)
    tables = set(inspect(engine).get_table_names())
    workflow_columns = {
        column["name"] for column in inspect(engine).get_columns("workflows")
    }
    stored = sa.table(
        "workflows",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
    )
    with engine.connect() as connection:
        name = connection.scalar(
            sa.select(stored.c.name).where(stored.c.id == workflow_id)
        )
    engine.dispose()
    get_settings.cache_clear()

    assert name == "Legacy Synthetic Workflow"
    assert "organization_id" not in workflow_columns
    assert "organizations" not in tables
    assert "organization_memberships" not in tables


def _dockerignore_excludes(pattern: str, required_path: str) -> bool:
    if pattern.startswith("!"):
        return False
    normalized = pattern.lstrip("/").rstrip("/")
    if normalized.endswith("/**"):
        normalized = normalized.removesuffix("/**").rstrip("/")
    return normalized in {required_path, f"**/{required_path}"}
