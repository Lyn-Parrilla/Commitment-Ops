"""Opt-in PostgreSQL integration-test lifecycle.

The harness reads only ``TEST_DATABASE_URL``. It creates one new schema, migrates
that schema, and drops only that schema after confirming the session is still
inside it. It never uses ``DATABASE_URL`` and never drops ``public``.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import NullPool

from app.persistence.database import (
    Base,
    create_database_engine,
    normalize_database_url,
    quoted_isolated_schema,
    redact_connection_secrets,
    validate_isolated_test_schema,
)
from app.persistence.exceptions import DatabaseConfigurationError, PersistenceError
from app.persistence.orm import OrganizationRecord

TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"
_TABLE_NAME = re.compile(r"^[a-z_]+$")
LEGACY_ORGANIZATION_ID = UUID("00000000-0000-0000-0000-000000000001")
SKIP_REASON = (
    "PostgreSQL integration tests were skipped because TEST_DATABASE_URL is "
    "not set. Export that variable, or set only that key in the untracked "
    ".env file, to a PostgreSQL database where a new schema can be created. "
    "DATABASE_URL is not used. SQLite unit tests do not need this variable."
)


class UnsafeTestDatabase(RuntimeError):
    """Raised when a PostgreSQL test target is not explicitly isolated."""

    def __repr__(self) -> str:
        return "UnsafeTestDatabase()"


class IsolatedPostgresTarget:
    """Migration coordinates whose repr never includes the connection URL."""

    def __init__(self, url: str, schema: str) -> None:
        self.url = url
        self.schema = schema

    def __repr__(self) -> str:
        return "IsolatedPostgresTarget(schema=[isolated])"


def explicit_test_database_url() -> str | None:
    """Return only ``TEST_DATABASE_URL``, never the application database URL."""

    configured = os.environ.get(TEST_DATABASE_URL_ENV)
    if configured is not None and configured.strip():
        return configured.strip()
    return _dotenv_test_database_url()


def _dotenv_test_database_url(path: Path | None = None) -> str | None:
    env_path = path or Path(".env")
    if not env_path.is_file():
        return None
    prefix = f"{TEST_DATABASE_URL_ENV}="
    try:
        lines = env_path.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        if not stripped.startswith(prefix):
            continue
        value = stripped[len(prefix) :].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value.strip() or None
    return None


def assert_postgres_test_url(url: str) -> str:
    """Validate the explicit test URL without treating the word test as proof."""

    try:
        normalized = normalize_database_url(url.strip())
        parsed = make_url(normalized)
    except Exception as exc:
        raise UnsafeTestDatabase(
            "TEST_DATABASE_URL is not a usable PostgreSQL URL"
        ) from PersistenceError(redact_connection_secrets(str(exc)))
    if parsed.drivername != "postgresql+psycopg":
        raise UnsafeTestDatabase(
            "TEST_DATABASE_URL must be a PostgreSQL URL. SQLite and "
            "DATABASE_URL are not used for these tests."
        )
    if not parsed.database or parsed.database.lower() in {"template0", "template1"}:
        raise UnsafeTestDatabase(
            "TEST_DATABASE_URL must name a non-template database"
        )
    return normalized


def new_test_schema() -> str:
    return validate_isolated_test_schema(f"commitmentops_test_{uuid4().hex[:16]}")


def isolated_schema_statement(schema: str, operation: str) -> str:
    quoted = quoted_isolated_schema(schema)
    if operation == "create":
        return f"CREATE SCHEMA {quoted}"
    if operation == "drop":
        return f"DROP SCHEMA {quoted} CASCADE"
    if operation == "search_path":
        return f"SET search_path TO {quoted}"
    raise DatabaseConfigurationError("unsupported schema operation")


def pin_test_search_path(url: str, schema: str) -> str:
    """Return a URL whose startup search_path is the isolated schema."""

    validated = validate_isolated_test_schema(schema)
    parsed = make_url(url)
    query = dict(parsed.query)
    query["options"] = f"-csearch_path={validated}"
    updated = URL.create(
        drivername=parsed.drivername,
        username=parsed.username,
        password=parsed.password,
        host=parsed.host,
        port=parsed.port,
        database=parsed.database,
        query=query,
    )
    return updated.render_as_string(hide_password=False)


def bind_test_target(engine: Engine, target: IsolatedPostgresTarget) -> None:
    engine._commitmentops_test_target = target  # type: ignore[attr-defined]


def require_test_target(engine: Engine) -> IsolatedPostgresTarget:
    target = getattr(engine, "_commitmentops_test_target", None)
    if not isinstance(target, IsolatedPostgresTarget):
        raise UnsafeTestDatabase("PostgreSQL test engine has no isolated target")
    return target


def alembic_config(target: IsolatedPostgresTarget) -> Config:
    config = Config("alembic.ini")
    pinned = pin_test_search_path(target.url, target.schema)
    config.set_main_option("sqlalchemy.url", pinned.replace("%", "%%"))
    config.attributes["commitmentops_test_schema"] = target.schema
    return config


@contextmanager
def isolated_postgres_engine(url: str) -> Iterator[Engine]:
    """Create, migrate, and drop one new schema for a single test module."""

    normalized = assert_postgres_test_url(url)
    schema = new_test_schema()
    admin = create_engine(
        normalized,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"connect_timeout": 5},
    )
    engine: Engine | None = None
    created = False

    def cleanup() -> None:
        pending = sys.exc_info()[1]
        try:
            if engine is not None:
                engine.dispose()
            if created:
                drop_isolated_schema(admin, schema)
        except Exception:
            if pending is None:
                raise
        finally:
            admin.dispose()

    try:
        _create_schema(admin, schema)
        created = True
        engine = create_database_engine(
            normalized,
            schema=schema,
            connect_args={"connect_timeout": 5},
        )
        target = IsolatedPostgresTarget(normalized, schema)
        bind_test_target(engine, target)
        command.upgrade(alembic_config(target), "head")
        command.check(alembic_config(target))
        _assert_migrated(engine, schema)
        if engine is None:
            raise UnsafeTestDatabase("PostgreSQL test engine was not created")
    except UnsafeTestDatabase:
        cleanup()
        raise
    except Exception as exc:
        cleanup()
        redacted = redact_connection_secrets(str(exc))
        raise UnsafeTestDatabase(
            "PostgreSQL integration setup failed closed and did not "
            f"modify the public schema. {redacted}"
        ) from PersistenceError(redacted)
    try:
        yield engine
    finally:
        cleanup()


def qualified_delete_statement(schema: str, table_name: str) -> str:
    if _TABLE_NAME.fullmatch(table_name) is None:
        raise UnsafeTestDatabase("refusing to delete an unexpected table")
    try:
        quoted_schema = quoted_isolated_schema(schema)
    except DatabaseConfigurationError as exc:
        raise UnsafeTestDatabase(
            "refusing to delete rows outside the isolated test schema"
        ) from exc
    return f"DELETE FROM {quoted_schema}.{table_name}"


def drop_isolated_schema(admin: Engine, schema: str) -> None:
    statement = isolated_schema_statement(schema, "drop")
    identifier = statement.split('"')[1]
    if identifier != schema or identifier == "public":
        raise UnsafeTestDatabase("refusing to drop a non-isolated schema")
    try:
        with admin.connect() as connection:
            _assert_cleanup_target(connection, schema)
            connection.exec_driver_sql(statement)
            connection.commit()
    except UnsafeTestDatabase:
        raise
    except Exception as exc:
        raise UnsafeTestDatabase(
            "isolated schema cleanup failed closed"
        ) from PersistenceError(redact_connection_secrets(str(exc)))


def _assert_cleanup_target(connection: object, schema: str) -> None:
    public_present = connection.scalar(  # type: ignore[attr-defined]
        text(
            "SELECT 1 FROM information_schema.schemata "
            "WHERE schema_name = 'public'"
        )
    )
    if public_present != 1:
        raise UnsafeTestDatabase(
            "refusing schema cleanup because the public schema is not present"
        )
    cross_schema_references = connection.scalar(  # type: ignore[attr-defined]
        text(
            "SELECT COUNT(*) FROM pg_constraint AS foreign_key "
            "JOIN pg_class AS source_table "
            "ON source_table.oid = foreign_key.conrelid "
            "JOIN pg_namespace AS source_schema "
            "ON source_schema.oid = source_table.relnamespace "
            "JOIN pg_class AS target_table "
            "ON target_table.oid = foreign_key.confrelid "
            "JOIN pg_namespace AS target_schema "
            "ON target_schema.oid = target_table.relnamespace "
            "WHERE foreign_key.contype = 'f' "
            "AND target_schema.nspname = :schema "
            "AND source_schema.nspname <> :schema"
        ),
        {"schema": schema},
    )
    if cross_schema_references is None or cross_schema_references:
        raise UnsafeTestDatabase(
            "refusing to drop a schema referenced by another schema"
        )
    connection.exec_driver_sql(  # type: ignore[attr-defined]
        isolated_schema_statement(schema, "search_path")
    )
    current = connection.scalar(text("SELECT current_schema()"))  # type: ignore[attr-defined]
    catalog_name = connection.scalar(  # type: ignore[attr-defined]
        text(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name = :schema"
        ),
        {"schema": schema},
    )
    if current != schema or catalog_name != schema:
        raise UnsafeTestDatabase(
            "refusing to drop a schema that is not the isolated test schema"
        )
    if isolated_schema_statement(schema, "drop").split('"')[1] != schema:
        raise UnsafeTestDatabase("refusing to drop a non-isolated schema")


def clear_isolated_tables(engine: Engine) -> None:
    target = require_test_target(engine)
    with engine.begin() as connection:
        current = connection.scalar(text("SELECT current_schema()"))
        if current != target.schema:
            raise UnsafeTestDatabase(
                "refusing to delete rows outside the isolated test schema"
            )
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(
                text(qualified_delete_statement(target.schema, table.name))
            )


def _create_schema(admin: Engine, schema: str) -> None:
    statement = isolated_schema_statement(schema, "create")
    try:
        with admin.connect() as connection:
            existing = connection.scalar(
                text(
                    "SELECT 1 FROM information_schema.schemata "
                    "WHERE schema_name = :schema"
                ),
                {"schema": schema},
            )
            if existing is not None:
                raise UnsafeTestDatabase(
                    "refusing to reuse an existing database schema"
                )
            connection.exec_driver_sql(statement)
            connection.commit()
            created = connection.scalar(
                text(
                    "SELECT 1 FROM information_schema.schemata "
                    "WHERE schema_name = :schema"
                ),
                {"schema": schema},
            )
            if created != 1:
                raise UnsafeTestDatabase(
                    "the isolated test schema was not created"
                )
    except UnsafeTestDatabase:
        raise
    except Exception as exc:
        raise UnsafeTestDatabase(
            "could not create an isolated PostgreSQL test schema"
        ) from PersistenceError(redact_connection_secrets(str(exc)))


def _assert_migrated(engine: Engine, schema: str) -> None:
    with engine.connect() as connection:
        current = connection.scalar(text("SELECT current_schema()"))
        if current != schema:
            raise UnsafeTestDatabase(
                "PostgreSQL connection is not using the isolated test schema"
            )
        version = connection.scalar(text("SELECT version_num FROM alembic_version"))
        if version != "20261014_02":
            raise UnsafeTestDatabase(
                "isolated schema did not migrate to the expected Alembic head"
            )
        legacy_name = connection.scalar(
            select(OrganizationRecord.name).where(
                OrganizationRecord.id == LEGACY_ORGANIZATION_ID
            )
        )
        if legacy_name != "Legacy Unassigned":
            raise UnsafeTestDatabase(
                "legacy organization was not preserved in the isolated schema"
            )
