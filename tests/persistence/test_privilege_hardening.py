"""Supabase API roles lose evidence-function and alembic_version access."""

from __future__ import annotations

from uuid import uuid4

from alembic import command
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from tests.persistence.postgres_support import (
    EXPECTED_ALEMBIC_HEAD,
    create_test_login,
)
from tests.persistence.test_clean_postgres_migration import (
    _drop_clean_database,
    _engine,
    _ident,
    _install_supabase_style_pgcrypto,
    _loopback_admin_url,
    _migration_config,
    _prepare_existing_shared_roles,
    _release_borrowed_shared_roles,
    _url_for,
)

pytestmark = pytest.mark.postgres

_API_ROLES = ("anon", "authenticated", "service_role")
_SUBMISSION_FUNCTIONS = (
    "record_evidence_storage_failure(text, text)",
    "release_evidence_object_reservation(text, text)",
    "reserve_evidence_object(text, uuid, text)",
    "submit_public_evidence_with_file(text, text, text, text, uuid, text, "
    "text, text, integer, text)",
)
_MAINTENANCE_FUNCTIONS = (
    "claim_evidence_storage_key(integer)",
    "complete_evidence_storage_cleanup(text, uuid)",
    "confirm_evidence_storage_claim(text, uuid)",
)
_RESTRICTED_FUNCTIONS = _SUBMISSION_FUNCTIONS + _MAINTENANCE_FUNCTIONS


def test_supabase_default_grants_are_removed_and_alembic_still_runs() -> None:
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
    created_api_roles: list[str] = []
    try:
        with admin.connect() as connection:
            migrator_password = create_test_login(
                connection,
                migrator,
                "LOGIN NOSUPERUSER BYPASSRLS CREATEROLE NOCREATEDB",
            )
            runtime_password = create_test_login(
                connection,
                runtime,
                "LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOINHERIT",
            )
            connection.execute(
                text(f"CREATE DATABASE {_ident(database)} OWNER {_ident(migrator)}")
            )
        if migrator_password is None or runtime_password is None:
            raise RuntimeError("refusing a test login without a password")
        _install_supabase_style_pgcrypto(admin_url, database, migrator)
        created_api_roles = _create_api_roles(admin)
        migrator_url = _url_for(
            admin_url, database, migrator, migrator_password
        )
        migrated = _engine(migrator_url)
        config = _migration_config(migrator_url, runtime)
        _install_default_function_grants(migrated)
        _prepare_existing_shared_roles(admin, migrator)
        command.upgrade(config, "20261027_09")
        _release_borrowed_shared_roles(admin, migrator)
        _assert_execute(migrated, "anon", "claim_evidence_storage_key(integer)", True)
        _grant_alembic_version(migrated)
        _prepare_existing_shared_roles(admin, migrator)
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        _release_borrowed_shared_roles(admin, migrator)
        runtime_engine = _engine(
            _url_for(admin_url, database, runtime, runtime_password)
        )
        try:
            _assert_hardened_privileges(migrated, runtime, runtime_engine)
            _prepare_existing_shared_roles(admin, migrator)
            command.downgrade(config, "20261027_09")
            _release_borrowed_shared_roles(admin, migrator)
            _assert_downgrade_keeps_the_revokes(migrated)
            _prepare_existing_shared_roles(admin, migrator)
            command.upgrade(config, "head")
            _release_borrowed_shared_roles(admin, migrator)
            _assert_hardened_privileges(migrated, runtime, runtime_engine)
        finally:
            runtime_engine.dispose()
    finally:
        if migrated is not None:
            _clear_default_function_grants(migrated)
            migrated.dispose()
        _drop_clean_database(admin, database, migrator, runtime)
        _drop_api_roles(admin, created_api_roles)
        admin.dispose()


def _create_api_roles(admin) -> list[str]:
    """Create missing API roles and return only the roles this test created."""

    created: list[str] = []
    try:
        with admin.connect() as connection:
            for role in _API_ROLES:
                exists = connection.scalar(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
                    {"role": role},
                )
                if exists:
                    continue
                create_test_login(
                    connection,
                    role,
                    "NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE",
                )
                created.append(role)
    except Exception:
        _drop_api_roles(admin, created)
        raise
    return created


def _install_default_function_grants(engine) -> None:
    with engine.begin() as connection:
        for role in _API_ROLES:
            connection.execute(
                text(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    f"GRANT EXECUTE ON FUNCTIONS TO {_quote_role(role)}"
                )
            )


def _grant_alembic_version(engine) -> None:
    with engine.begin() as connection:
        for role in _API_ROLES:
            connection.execute(
                text(
                    "GRANT ALL ON TABLE public.alembic_version "
                    f"TO {_quote_role(role)}"
                )
            )


def _assert_hardened_privileges(engine, runtime: str, runtime_engine) -> None:
    with engine.connect() as connection:
        version = connection.scalar(text("SELECT version_num FROM alembic_version"))
        rls = connection.execute(
            text(
                """
                SELECT relation.relrowsecurity, relation.relforcerowsecurity
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'public'
                  AND relation.relname = 'alembic_version'
                """
            )
        ).one()
        policy_count = connection.scalar(
            text(
                """
                SELECT count(*)
                FROM pg_policy
                WHERE polrelid = 'public.alembic_version'::regclass
                """
            )
        )
        forced_files = connection.scalar(
            text(
                """
                SELECT relation.relforcerowsecurity
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'public'
                  AND relation.relname = 'evidence_files'
                """
            )
        )
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
        submission_member = connection.scalar(
            text(
                """
                SELECT pg_has_role(
                    current_user, 'commitmentops_submission', 'MEMBER'
                )
                """
            )
        )
        maintenance_member = connection.scalar(
            text(
                """
                SELECT pg_has_role(
                    current_user, 'commitmentops_maintenance', 'MEMBER'
                )
                """
            )
        )
        confirm_owner = connection.scalar(
            text(
                """
                SELECT pg_get_userbyid(routine.proowner)
                FROM pg_proc AS routine
                JOIN pg_namespace AS namespace
                  ON namespace.oid = routine.pronamespace
                WHERE namespace.nspname = 'public'
                  AND routine.proname = 'confirm_evidence_storage_claim'
                """
            )
        )
    assert version == EXPECTED_ALEMBIC_HEAD
    assert tuple(rls) == (True, False)
    assert policy_count == 0
    assert forced_files is True
    assert confirm_owner == "commitmentops_submission"
    assert submission_member is False
    assert maintenance_member is False
    assert [tuple(row) for row in attributes] == [
        ("commitmentops_app", False, False, False, False, False),
        ("commitmentops_maintenance", False, False, False, False, False),
        ("commitmentops_submission", False, False, False, False, False),
    ]
    for function_name in _RESTRICTED_FUNCTIONS:
        for role in ("anon", "authenticated", "service_role", "public"):
            _assert_execute(engine, role, function_name, False)
    for function_name in _SUBMISSION_FUNCTIONS:
        _assert_execute(engine, "commitmentops_app", function_name, True)
        _assert_execute(engine, "commitmentops_maintenance", function_name, False)
    for function_name in _MAINTENANCE_FUNCTIONS:
        _assert_execute(engine, "commitmentops_maintenance", function_name, True)
        _assert_execute(engine, "commitmentops_app", function_name, False)
    _assert_table_denied(engine, "commitmentops_app", assume=True)
    _assert_table_denied(engine, runtime, assume=False)
    _assert_statements_denied(runtime_engine)


def _assert_downgrade_keeps_the_revokes(engine) -> None:
    with engine.connect() as connection:
        version = connection.scalar(text("SELECT version_num FROM alembic_version"))
        rls_enabled = connection.scalar(
            text(
                """
                SELECT relation.relrowsecurity
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'public'
                  AND relation.relname = 'alembic_version'
                """
            )
        )
        confirm_count = connection.scalar(
            text(
                """
                SELECT count(*)
                FROM pg_proc AS routine
                JOIN pg_namespace AS namespace
                  ON namespace.oid = routine.pronamespace
                WHERE namespace.nspname = 'public'
                  AND routine.proname = 'confirm_evidence_storage_claim'
                """
            )
        )
    assert version == "20261027_09"
    assert rls_enabled is False
    assert confirm_count == 0
    for function_name in _SUBMISSION_FUNCTIONS + _MAINTENANCE_FUNCTIONS[:2]:
        for role in ("anon", "authenticated", "service_role", "public"):
            _assert_execute(engine, role, function_name, False)


def _assert_execute(engine, role: str, signature: str, expected: bool) -> None:
    with engine.connect() as connection:
        allowed = connection.scalar(
            text(
                """
                SELECT has_function_privilege(
                    :role, :function_name, 'EXECUTE'
                )
                """
            ),
            {"role": role, "function_name": f"public.{signature}"},
        )
    assert allowed is expected


def _assert_table_denied(engine, role: str, *, assume: bool) -> None:
    quoted = '"commitmentops_app"' if role == "commitmentops_app" else None
    with engine.connect() as connection:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            allowed = connection.scalar(
                text(
                    """
                    SELECT has_table_privilege(
                        :role, 'public.alembic_version', :privilege
                    )
                    """
                ),
                {"role": role, "privilege": privilege},
            )
            assert allowed is False
        connection.rollback()
        if not assume:
            return
        transaction = connection.begin()
        try:
            connection.execute(text(f"SET LOCAL ROLE {quoted}"))
            with pytest.raises(SQLAlchemyError):
                with connection.begin_nested():
                    connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    )
            with pytest.raises(SQLAlchemyError):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            "UPDATE alembic_version SET version_num = version_num"
                        )
                    )
        finally:
            transaction.rollback()


def _assert_statements_denied(engine) -> None:
    with engine.connect() as connection:
        with pytest.raises(SQLAlchemyError):
            connection.execute(text("SELECT version_num FROM alembic_version"))
        connection.rollback()
        with pytest.raises(SQLAlchemyError):
            connection.execute(
                text("UPDATE alembic_version SET version_num = version_num")
            )
        connection.rollback()


def _clear_default_function_grants(engine) -> None:
    with engine.begin() as connection:
        for role in _API_ROLES:
            exists = connection.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
                {"role": role},
            )
            if exists != 1:
                continue
            connection.execute(
                text(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    f"REVOKE EXECUTE ON FUNCTIONS FROM {_quote_role(role)}"
                )
            )


def _drop_api_roles(admin, created: list[str]) -> None:
    """Drop only API roles created by this test. Shared roles stay."""

    with admin.connect() as connection:
        for role in created:
            if role not in _API_ROLES:
                raise RuntimeError("refusing an unexpected role name")
            connection.execute(text(f"DROP ROLE IF EXISTS {_quote_role(role)}"))


def _quote_role(role: str) -> str:
    if role not in _API_ROLES and role != "commitmentops_app":
        raise RuntimeError("refusing an unexpected role name")
    return f'"{role}"'
