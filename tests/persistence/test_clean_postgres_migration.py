"""Migrate an empty loopback database as a Supabase-like login.

The admin URL is only ``TEST_DATABASE_URL``. This module creates a new
database on that same local server, installs ``pgcrypto`` the way Supabase
does, and migrates ``public`` as a non-superuser with ``CREATEROLE`` and
``BYPASSRLS``. It refuses a non-loopback host before creating anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from app.domain.submissions import submission_token_hash
from app.persistence.database import RUNTIME_LOGIN_ATTRIBUTE
from tests.persistence.postgres_support import (
    EXPECTED_ALEMBIC_HEAD,
    SKIP_REASON,
    assert_postgres_test_url,
    explicit_test_database_url,
)

pytestmark = pytest.mark.postgres

_LOOPBACK = frozenset({"localhost", "127.0.0.1"})


def test_clean_database_reaches_head_as_a_non_superuser() -> None:
    admin_url = _loopback_admin_url()
    suffix = uuid4().hex[:12]
    database = f"commitmentops_clean_{suffix}"
    migrator = f"commitmentops_clean_migrator_{suffix}"
    runtime = f"commitmentops_clean_runtime_{suffix}"
    admin = create_engine(
        admin_url,
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
        connect_args={"connect_timeout": 5},
    )
    migrated = None
    try:
        with admin.connect() as connection:
            connection.execute(text(f"CREATE ROLE {_ident(migrator)} LOGIN"))
            connection.execute(
                text(
                    f"ALTER ROLE {_ident(migrator)} NOSUPERUSER BYPASSRLS "
                    "CREATEROLE NOCREATEDB"
                )
            )
            connection.execute(text(f"CREATE ROLE {_ident(runtime)} LOGIN"))
            connection.execute(
                text(
                    f"ALTER ROLE {_ident(runtime)} NOSUPERUSER NOBYPASSRLS "
                    "NOCREATEROLE NOCREATEDB NOINHERIT"
                )
            )
            connection.execute(
                text(f"CREATE DATABASE {_ident(database)} OWNER {_ident(migrator)}")
            )
        _install_supabase_style_pgcrypto(admin_url, database, migrator)
        migrator_url = _url_for(admin_url, database, migrator)
        migrated = _engine(migrator_url)
        config = _migration_config(migrator_url, runtime)
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        command.check(config)
        _assert_head_and_boundaries(migrated, runtime)
        _assert_public_submission_is_visible_only_through_the_function(migrated)
    finally:
        if migrated is not None:
            migrated.dispose()
        _drop_clean_database(admin, database, migrator, runtime)
        admin.dispose()


def _loopback_admin_url() -> str:
    configured = explicit_test_database_url()
    if configured is None:
        pytest.skip(SKIP_REASON)
    normalized = assert_postgres_test_url(configured)
    host = (make_url(normalized).host or "").lower()
    if host not in _LOOPBACK:
        pytest.fail(
            "clean-database migration refused a non-loopback test database"
        )
    return normalized


def _install_supabase_style_pgcrypto(
    admin_url: str,
    database: str,
    migrator: str,
) -> None:
    engine = _engine(_url_for(admin_url, database, None))
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE SCHEMA extensions"))
            connection.execute(
                text("CREATE EXTENSION pgcrypto SCHEMA extensions")
            )
            connection.execute(
                text(f"ALTER SCHEMA extensions OWNER TO {_ident(migrator)}")
            )
            connection.execute(
                text(
                    "ALTER FUNCTION extensions.digest(bytea, text) "
                    f"OWNER TO {_ident(migrator)}"
                )
            )
            connection.execute(
                text(f"ALTER SCHEMA public OWNER TO {_ident(migrator)}")
            )
    finally:
        engine.dispose()


def _assert_head_and_boundaries(engine, runtime: str) -> None:
    with engine.connect() as connection:
        version = connection.scalar(text("SELECT version_num FROM alembic_version"))
        attributes = connection.execute(
            text(
                """
                SELECT rolname, rolcanlogin, rolsuper, rolbypassrls,
                       rolcreatedb, rolcreaterole
                FROM pg_roles
                WHERE rolname IN (
                    'commitmentops_app',
                    'commitmentops_submission',
                    'commitmentops_maintenance'
                )
                ORDER BY rolname
                """
            )
        ).all()
        app_can_assume_submission = connection.scalar(
            text(
                """
                SELECT pg_has_role(
                    'commitmentops_app',
                    'commitmentops_submission',
                    'MEMBER'
                )
                """
            )
        )
        runtime_can_assume_app = connection.scalar(
            text("SELECT pg_has_role(:runtime, 'commitmentops_app', 'MEMBER')"),
            {"runtime": runtime},
        )
        submission_can_create = connection.scalar(
            text(
                """
                SELECT has_schema_privilege(
                    'commitmentops_submission', 'public', 'CREATE'
                )
                """
            )
        )
        source = connection.scalar(
            text(
                """
                SELECT prosrc
                FROM pg_proc
                JOIN pg_namespace ON pg_namespace.oid = pg_proc.pronamespace
                WHERE pg_namespace.nspname = 'public'
                  AND proname = 'public_submission_context'
                """
            )
        )
    assert version == EXPECTED_ALEMBIC_HEAD
    assert [tuple(row) for row in attributes] == [
        ("commitmentops_app", False, False, False, False, False),
        ("commitmentops_maintenance", False, False, False, False, False),
        ("commitmentops_submission", False, False, False, False, False),
    ]
    assert app_can_assume_submission is False
    assert runtime_can_assume_app is True
    assert submission_can_create is False
    assert source is not None
    assert '"extensions".digest(' in source
    assert "commitmentops_crypto" not in source


def _assert_public_submission_is_visible_only_through_the_function(engine) -> None:
    organization_id = uuid4()
    workflow_id = uuid4()
    requirement_id = uuid4()
    person_id = uuid4()
    link_id = uuid4()
    token = "clean-migration-token"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO organizations (id, name, display_name)
                VALUES (:id, 'Clean migration', 'Clean migration org')
                """
            ),
            {"id": organization_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO people (id, name, email)
                VALUES (:id, 'Clean owner', 'clean-owner@example.test')
                """
            ),
            {"id": person_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO workflows (id, organization_id, name, workflow_type, status)
                VALUES (:id, :organization_id, 'Clean workflow', 'general', 'active')
                """
            ),
            {"id": workflow_id, "organization_id": organization_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO requirements (
                    id, workflow_id, title, description, owner_id,
                    evidence_expectation, provenance, status, review_status
                )
                VALUES (
                    :id, :workflow_id, 'Clean requirement', 'Describe it',
                    :owner_id, 'A file', 'user_provided', 'not_started',
                    'approved'
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
                """
                INSERT INTO submission_links (
                    id, organization_id, workflow_id, requirement_id, token_hash,
                    created_at, expires_at, created_by_user_id, max_uses, use_count
                )
                VALUES (
                    :id, :organization_id, :workflow_id, :requirement_id, :token_hash,
                    :created_at, :expires_at, :created_by_user_id, 25, 0
                )
                """
            ),
            {
                "id": link_id,
                "organization_id": organization_id,
                "workflow_id": workflow_id,
                "requirement_id": requirement_id,
                "token_hash": submission_token_hash(token),
                "created_at": now,
                "expires_at": now + timedelta(days=1),
                "created_by_user_id": person_id,
            },
        )
        connection.execute(text("SET ROLE commitmentops_app"))
        visible = connection.execute(
            text(
                """
                SELECT organization_name, requirement_title
                FROM public_submission_context(:token)
                """
            ),
            {"token": token},
        ).one()
        with pytest.raises(SQLAlchemyError):
            with connection.begin_nested():
                connection.execute(text("SELECT token_hash FROM submission_links"))
    assert tuple(visible) == ("Clean migration org", "Clean requirement")


def _migration_config(url: str, runtime: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    config.attributes[RUNTIME_LOGIN_ATTRIBUTE] = runtime
    return config


def _url_for(admin_url: str, database: str, username: str | None) -> str:
    updated = make_url(admin_url).set(database=database)
    if username is not None:
        updated = updated.set(username=username, password=None)
    return updated.render_as_string(hide_password=False)


def _engine(url: str):
    return create_engine(
        url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"connect_timeout": 5},
    )


def _drop_clean_database(admin, database: str, migrator: str, runtime: str) -> None:
    with admin.connect() as connection:
        connection.execute(
            text(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = :database
                  AND pid <> pg_backend_pid()
                """
            ),
            {"database": database},
        )
        connection.execute(text(f"DROP DATABASE IF EXISTS {_ident(database)}"))
        for member in (migrator, runtime):
            for granted in (
                "commitmentops_app",
                "commitmentops_submission",
                "commitmentops_maintenance",
            ):
                _revoke_membership(connection, member, granted)
        connection.execute(text(f"DROP ROLE IF EXISTS {_ident(runtime)}"))
        connection.execute(text(f"DROP ROLE IF EXISTS {_ident(migrator)}"))


def _revoke_membership(connection, member: str, granted: str) -> None:
    belongs = connection.scalar(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_auth_members AS membership
                JOIN pg_roles AS granted_role
                  ON granted_role.oid = membership.roleid
                JOIN pg_roles AS member_role
                  ON member_role.oid = membership.member
                WHERE granted_role.rolname = :granted
                  AND member_role.rolname = :member
            )
            """
        ),
        {"granted": granted, "member": member},
    )
    if belongs:
        connection.execute(
            text(f"REVOKE {_known_role(granted)} FROM {_ident(member)}")
        )


def _known_role(value: str) -> str:
    if value not in {
        "commitmentops_app",
        "commitmentops_submission",
        "commitmentops_maintenance",
    }:
        raise RuntimeError("refusing an unexpected role name")
    return f'"{value}"'


def _ident(value: str) -> str:
    compact = value.replace("_", "")
    if not value.startswith("commitmentops_clean_") or not compact.isalnum():
        raise RuntimeError("refusing an unexpected clean-database identifier")
    return f'"{value}"'
