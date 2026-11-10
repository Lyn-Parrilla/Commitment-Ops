"""Supabase API roles lose evidence-function, private-table, and default access."""

from __future__ import annotations

import importlib.util
from pathlib import Path
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


_PRIVATE_TABLES = (
    "evidence_files",
    "evidence_file_reservations",
    "evidence_storage_cleanup",
    "source_documents",
)
_TABLE_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)
_APP_EVIDENCE_FILE_COLUMNS = (
    "id",
    "evidence_id",
    "organization_id",
    "original_filename",
    "media_type",
    "byte_size",
    "checksum_sha256",
    "uploaded_at",
    "scan_status",
)
_EXPECTED_POLICIES = {
    "evidence_files": frozenset(
        {"evidence_files_tenant", "submission_definer_access"}
    ),
    "evidence_file_reservations": frozenset({"submission_definer_access"}),
    "evidence_storage_cleanup": frozenset({"submission_definer_access"}),
    "source_documents": frozenset({"source_documents_tenant"}),
}


def test_api_roles_lose_private_table_privileges_and_downgrade_keeps_the_revokes() -> None:
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
            create_test_login(
                connection,
                runtime,
                "LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOINHERIT",
            )
            connection.execute(
                text(f"CREATE DATABASE {_ident(database)} OWNER {_ident(migrator)}")
            )
        if migrator_password is None:
            raise RuntimeError("refusing a test login without a password")
        _install_supabase_style_pgcrypto(admin_url, database, migrator)
        created_api_roles = _create_api_roles(admin)
        migrator_url = _url_for(
            admin_url, database, migrator, migrator_password
        )
        migrated = _engine(migrator_url)
        config = _migration_config(migrator_url, runtime)
        _prepare_existing_shared_roles(admin, migrator)
        command.upgrade(config, "20261103_11")
        _release_borrowed_shared_roles(admin, migrator)
        _grant_api_table_privileges(migrated)
        _assert_api_table_privileges(migrated, allowed=True)
        command.upgrade(config, "head")
        _assert_api_table_privileges(migrated, allowed=False)
        _assert_internal_table_privileges(migrated)
        _assert_private_tables_force_rls(migrated)
        _assert_missing_table_or_role_fails(admin, migrated)
        command.downgrade(config, "20261103_11")
        _assert_api_table_privileges(migrated, allowed=False)
        _assert_internal_table_privileges(migrated)
        _assert_private_tables_force_rls(migrated)
    finally:
        if migrated is not None:
            migrated.dispose()
        _drop_clean_database(admin, database, migrator, runtime)
        _drop_api_roles(admin, created_api_roles)
        admin.dispose()


def _grant_api_table_privileges(engine) -> None:
    with engine.begin() as connection:
        for role in _API_ROLES:
            for table in _PRIVATE_TABLES:
                connection.execute(
                    text(
                        "GRANT ALL ON TABLE "
                        f"{_qualified_table(table)} TO {_quote_role(role)}"
                    )
                )


def _assert_api_table_privileges(engine, *, allowed: bool) -> None:
    with engine.connect() as connection:
        for role in _API_ROLES:
            for table in _PRIVATE_TABLES:
                for privilege in _TABLE_PRIVILEGES:
                    actual = connection.scalar(
                        text(
                            """
                            SELECT has_table_privilege(
                                :role, :table_name, :privilege
                            )
                            """
                        ),
                        {
                            "role": role,
                            "table_name": f"public.{table}",
                            "privilege": privilege,
                        },
                    )
                    assert actual is allowed


def _assert_internal_table_privileges(engine) -> None:
    with engine.connect() as connection:
        _assert_privileges(
            connection,
            "commitmentops_app",
            "evidence_files",
            allowed=set(),
        )
        for column in _APP_EVIDENCE_FILE_COLUMNS:
            assert _column_privilege(
                connection,
                "commitmentops_app",
                "evidence_files",
                column,
                "SELECT",
            ) is True
        assert _column_privilege(
            connection,
            "commitmentops_app",
            "evidence_files",
            "storage_key",
            "SELECT",
        ) is False
        for table in ("evidence_file_reservations", "evidence_storage_cleanup"):
            _assert_privileges(
                connection,
                "commitmentops_app",
                table,
                allowed=set(),
            )
        _assert_privileges(
            connection,
            "commitmentops_app",
            "source_documents",
            allowed={"SELECT", "INSERT", "UPDATE"},
        )
        _assert_privileges(
            connection,
            "commitmentops_submission",
            "evidence_files",
            allowed={"SELECT", "INSERT"},
        )
        _assert_privileges(
            connection,
            "commitmentops_submission",
            "evidence_file_reservations",
            allowed={"SELECT", "INSERT", "DELETE"},
        )
        _assert_privileges(
            connection,
            "commitmentops_submission",
            "evidence_storage_cleanup",
            allowed={"SELECT", "INSERT", "DELETE"},
        )
        for column in ("claimed_at", "claim_token"):
            assert _column_privilege(
                connection,
                "commitmentops_submission",
                "evidence_storage_cleanup",
                column,
                "UPDATE",
            ) is True
        for column in ("storage_key", "organization_id", "recorded_at"):
            assert _column_privilege(
                connection,
                "commitmentops_submission",
                "evidence_storage_cleanup",
                column,
                "UPDATE",
            ) is False
        _assert_privileges(
            connection,
            "commitmentops_submission",
            "source_documents",
            allowed=set(),
        )
        for table in _PRIVATE_TABLES:
            _assert_privileges(
                connection,
                "commitmentops_maintenance",
                table,
                allowed=set(),
            )


def _assert_private_tables_force_rls(engine) -> None:
    with engine.connect() as connection:
        for table, policies in _EXPECTED_POLICIES.items():
            flags = connection.execute(
                text(
                    """
                    SELECT relation.relrowsecurity,
                           relation.relforcerowsecurity
                    FROM pg_class AS relation
                    JOIN pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = 'public'
                      AND relation.relname = :table_name
                    """
                ),
                {"table_name": table},
            ).one()
            names = set(
                connection.scalars(
                    text(
                        """
                        SELECT policy.polname
                        FROM pg_policy AS policy
                        JOIN pg_class AS relation
                          ON relation.oid = policy.polrelid
                        JOIN pg_namespace AS namespace
                          ON namespace.oid = relation.relnamespace
                        WHERE namespace.nspname = 'public'
                          AND relation.relname = :table_name
                        """
                    ),
                    {"table_name": table},
                ).all()
            )
            assert tuple(flags) == (True, True)
            assert names == policies


def _assert_missing_table_or_role_fails(admin, engine) -> None:
    statement = _revoke_sql()
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(
                text(
                    "ALTER TABLE public.evidence_files "
                    "RENAME TO evidence_files_hidden"
                )
            )
            with pytest.raises(
                SQLAlchemyError,
                match="required table evidence_files is missing",
            ):
                connection.execute(text(statement))
        finally:
            transaction.rollback()
    with admin.connect() as connection:
        connection.execute(
            text(
                "ALTER ROLE commitmentops_maintenance "
                "RENAME TO commitmentops_maintenance_hidden"
            )
        )
    try:
        with engine.connect() as connection:
            with pytest.raises(
                SQLAlchemyError,
                match="required role commitmentops_maintenance is missing",
            ):
                connection.execute(text(statement))
            connection.rollback()
    finally:
        with admin.connect() as connection:
            connection.execute(
                text(
                    "ALTER ROLE commitmentops_maintenance_hidden "
                    "RENAME TO commitmentops_maintenance"
                )
            )


def _assert_privileges(connection, role: str, table: str, *, allowed: set[str]) -> None:
    for privilege in _TABLE_PRIVILEGES:
        actual = connection.scalar(
            text(
                """
                SELECT has_table_privilege(:role, :table_name, :privilege)
                """
            ),
            {
                "role": role,
                "table_name": f"public.{table}",
                "privilege": privilege,
            },
        )
        assert actual is (privilege in allowed)


def _column_privilege(connection, role: str, table: str, column: str, privilege: str):
    return connection.scalar(
        text(
            """
            SELECT has_column_privilege(
                :role, :table_name, :column_name, :privilege
            )
            """
        ),
        {
            "role": role,
            "table_name": f"public.{table}",
            "column_name": column,
            "privilege": privilege,
        },
    )


def _qualified_table(table: str) -> str:
    if table not in _PRIVATE_TABLES:
        raise RuntimeError("refusing an unexpected table name")
    return f'"public"."{table}"'


_PROBE_TABLES = (
    "default_probe_before",
    "default_probe_after",
    "default_probe_downgrade",
)
_DEFAULT_SCOPES = ("global", "schema")


def test_future_tables_do_not_inherit_api_default_privileges() -> None:
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
            create_test_login(
                connection,
                runtime,
                "LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOINHERIT",
            )
            connection.execute(
                text(f"CREATE DATABASE {_ident(database)} OWNER {_ident(migrator)}")
            )
        if migrator_password is None:
            raise RuntimeError("refusing a test login without a password")
        _install_supabase_style_pgcrypto(admin_url, database, migrator)
        created_api_roles = _create_api_roles(admin)
        migrator_url = _url_for(
            admin_url, database, migrator, migrator_password
        )
        migrated = _engine(migrator_url)
        config = _migration_config(migrator_url, runtime)
        _assert_migration_login(migrated, migrator)
        _install_default_table_grants(migrated)
        _assert_default_table_acl(migrated, granted=True)
        _prepare_existing_shared_roles(admin, migrator)
        command.upgrade(config, "20261104_12")
        _release_borrowed_shared_roles(admin, migrator)
        _assert_alembic_version(migrated, "20261104_12")
        _assert_migration_login(migrated, migrator)
        _assert_tables_owned_by(migrated, _PRIVATE_TABLES, migrator)
        _assert_api_table_privileges(migrated, allowed=False)
        _assert_internal_table_privileges(migrated)
        _assert_private_tables_force_rls(migrated)
        _create_probe(migrated, "default_probe_before")
        _assert_tables_owned_by(migrated, ("default_probe_before",), migrator)
        _assert_probe_privileges(migrated, "default_probe_before", allowed=True)
        _assert_migration_login(migrated, migrator)
        command.upgrade(config, "20261106_13")
        _assert_alembic_version(migrated, "20261106_13")
        _assert_migration_login(migrated, migrator)
        _assert_default_table_acl(migrated, granted=False)
        _create_probe(migrated, "default_probe_after")
        _assert_tables_owned_by(migrated, ("default_probe_after",), migrator)
        _assert_probe_privileges(migrated, "default_probe_after", allowed=False)
        _assert_tables_owned_by(migrated, _PRIVATE_TABLES, migrator)
        _assert_api_table_privileges(migrated, allowed=False)
        _assert_internal_table_privileges(migrated)
        _assert_private_tables_force_rls(migrated)
        command.downgrade(config, "20261104_12")
        _assert_alembic_version(migrated, "20261104_12")
        _assert_migration_login(migrated, migrator)
        _assert_default_table_acl(migrated, granted=False)
        _create_probe(migrated, "default_probe_downgrade")
        _assert_tables_owned_by(
            migrated, ("default_probe_downgrade",), migrator
        )
        _assert_probe_privileges(
            migrated, "default_probe_downgrade", allowed=False
        )
        _assert_tables_owned_by(migrated, _PRIVATE_TABLES, migrator)
        _assert_api_table_privileges(migrated, allowed=False)
        _assert_internal_table_privileges(migrated)
        _assert_private_tables_force_rls(migrated)
    finally:
        try:
            if migrated is not None:
                try:
                    _clear_default_table_grants(migrated)
                    _drop_probe_tables(migrated)
                finally:
                    migrated.dispose()
        finally:
            _drop_clean_database(admin, database, migrator, runtime)
            _drop_api_roles(admin, created_api_roles)
            admin.dispose()


def _assert_alembic_version(engine, version: str) -> None:
    with engine.connect() as connection:
        actual = connection.scalar(
            text("SELECT version_num FROM alembic_version")
        )
    assert actual == version


def _assert_migration_login(engine, migrator: str) -> None:
    with engine.connect() as connection:
        current_name, session_name, schema_name = connection.execute(
            text("SELECT current_user, session_user, current_schema()")
        ).one()
    if current_name != session_name or current_name != migrator:
        raise AssertionError("migration login must match the session login")
    if schema_name != "public":
        raise AssertionError("disposable migration schema must be public")


def _install_default_table_grants(engine) -> None:
    with engine.begin() as connection:
        for role in _API_ROLES:
            quoted = _quote_role(role)
            connection.execute(
                text(
                    "ALTER DEFAULT PRIVILEGES "
                    f"GRANT ALL ON TABLES TO {quoted}"
                )
            )
            connection.execute(
                text(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    f"GRANT ALL ON TABLES TO {quoted}"
                )
            )


def _clear_default_table_grants(engine) -> None:
    with engine.begin() as connection:
        for role in _API_ROLES:
            exists = connection.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
                {"role": role},
            )
            if exists != 1:
                continue
            quoted = _quote_role(role)
            connection.execute(
                text(
                    "ALTER DEFAULT PRIVILEGES "
                    f"REVOKE ALL ON TABLES FROM {quoted}"
                )
            )
            connection.execute(
                text(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    f"REVOKE ALL ON TABLES FROM {quoted}"
                )
            )


def _assert_default_table_acl(engine, *, granted: bool) -> None:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT CASE
                           WHEN entry.defaclnamespace = 0 THEN 'global'
                           ELSE 'schema'
                       END AS scope,
                       COALESCE(grantee.rolname, 'PUBLIC') AS grantee,
                       acl.privilege_type
                  FROM pg_default_acl AS entry
                  JOIN pg_roles AS owner
                    ON owner.oid = entry.defaclrole
                  LEFT JOIN pg_namespace AS namespace
                    ON namespace.oid = entry.defaclnamespace
                  LEFT JOIN LATERAL aclexplode(entry.defaclacl) AS acl
                    ON true
                  LEFT JOIN pg_roles AS grantee
                    ON grantee.oid = acl.grantee
                 WHERE owner.rolname = current_user
                   AND entry.defaclobjtype = 'r'
                   AND (
                       entry.defaclnamespace = 0
                       OR namespace.nspname = current_schema()
                   )
                """
            )
        ).all()
    found = {
        (scope, grantee, privilege)
        for scope, grantee, privilege in rows
        if privilege is not None
    }
    for scope in _DEFAULT_SCOPES:
        for role in _API_ROLES:
            for privilege in _TABLE_PRIVILEGES:
                assert ((scope, role, privilege) in found) is granted


def _create_probe(engine, table: str) -> None:
    qualified = _probe_table(table)
    with engine.begin() as connection:
        connection.execute(text(f"CREATE TABLE {qualified} (id integer)"))


def _drop_probe_tables(engine) -> None:
    with engine.begin() as connection:
        for table in _PROBE_TABLES:
            connection.execute(
                text(f"DROP TABLE IF EXISTS {_probe_table(table)}")
            )


def _assert_tables_owned_by(engine, tables: tuple[str, ...], owner: str) -> None:
    with engine.connect() as connection:
        for table in tables:
            actual = connection.scalar(
                text(
                    """
                    SELECT pg_get_userbyid(relation.relowner)
                      FROM pg_class AS relation
                      JOIN pg_namespace AS namespace
                        ON namespace.oid = relation.relnamespace
                     WHERE namespace.nspname = 'public'
                       AND relation.relname = :table_name
                    """
                ),
                {"table_name": table},
            )
            assert actual == owner


def _assert_probe_privileges(engine, table: str, *, allowed: bool) -> None:
    _probe_table(table)
    with engine.connect() as connection:
        for role in _API_ROLES:
            for privilege in _TABLE_PRIVILEGES:
                actual = connection.scalar(
                    text(
                        """
                        SELECT has_table_privilege(
                            :role, :table_name, :privilege
                        )
                        """
                    ),
                    {
                        "role": role,
                        "table_name": f"public.{table}",
                        "privilege": privilege,
                    },
                )
                assert actual is allowed


def _probe_table(table: str) -> str:
    if table not in _PROBE_TABLES:
        raise RuntimeError("refusing an unexpected probe table")
    return f'"public"."{table}"'


def _revoke_sql() -> str:
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "20261104_12_private_table_privileges.py"
    )
    spec = importlib.util.spec_from_file_location("revision_20261104_12", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("private-table migration could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.private_table_revoke_sql("public")
