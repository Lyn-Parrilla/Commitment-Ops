"""Migrate an empty loopback database as a Supabase-like login.

The admin URL is only ``TEST_DATABASE_URL``. This module creates a new
database on that same local server, installs ``pgcrypto`` the way Supabase
does, and migrates ``public`` as a non-superuser with ``CREATEROLE`` and
``BYPASSRLS``. It refuses a non-loopback host before creating anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from secrets import token_hex
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from app.domain.evidence_files import build_storage_key
from app.domain.submissions import submission_token_hash
from app.persistence.database import RUNTIME_LOGIN_ATTRIBUTE
from tests.persistence.postgres_support import (
    EXPECTED_ALEMBIC_HEAD,
    SKIP_REASON,
    assert_postgres_test_url,
    create_test_login,
    explicit_test_database_url,
    grant_existing_shared_role_admin,
    temporary_role_url,
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
        migrator_url = _url_for(
            admin_url, database, migrator, migrator_password
        )
        migrated = _engine(migrator_url)
        config = _migration_config(migrator_url, runtime)
        _prepare_existing_shared_roles(admin, migrator)
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        _release_borrowed_shared_roles(admin, migrator)
        command.check(config)
        _assert_head_and_boundaries(migrated, runtime)
        _assert_public_submission_is_visible_only_through_the_function(migrated)
        _assert_definer_policy_is_required_and_role_scoped(migrated)
    finally:
        if migrated is not None:
            migrated.dispose()
        _drop_clean_database(admin, database, migrator, runtime)
        admin.dispose()


def _prepare_existing_shared_roles(admin, migrator: str) -> None:
    with admin.connect() as connection:
        grant_existing_shared_role_admin(connection, migrator)


def _release_borrowed_shared_roles(admin, migrator: str) -> None:
    """Remove the superuser grant the migration login cannot revoke itself.

    Migrations revoke membership they granted. A reused cluster records the
    temporary ADMIN grant as coming from the admin login, so that revoke
    leaves it behind. Drop only this migrator's submission and maintenance
    membership. The application role grant and the runtime login stay.
    """

    with admin.connect() as connection:
        for role in ("commitmentops_submission", "commitmentops_maintenance"):
            _revoke_membership(connection, migrator, role)


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


_DEFINER_TABLES = (
    "audit_events",
    "evidence",
    "evidence_file_reservations",
    "evidence_files",
    "evidence_storage_cleanup",
    "organizations",
    "requirements",
    "submission_links",
    "workflows",
)


def _assert_definer_policy_is_required_and_role_scoped(engine) -> None:
    """Prove the all-rows policy applies only to the definer role.

    The policy is ``FOR ALL`` with ``USING (true)``. Forced row-level
    security still applies because the owner has ``NOBYPASSRLS``. Callers
    who are not members of that role keep the tenant policies. Dropping the
    policy must make the submission function return nothing, which shows the
    caller’s bypass attribute does not leak into the function.
    """

    token = "clean-migration-token"
    with engine.begin() as connection:
        submission_oid = connection.scalar(
            text("SELECT oid FROM pg_roles WHERE rolname = 'commitmentops_submission'")
        )
        policies = connection.execute(
            text(
                """
                SELECT relation.relname AS table_name,
                       relation.relrowsecurity AS row_security,
                       relation.relforcerowsecurity AS forced,
                       policy.polcmd AS command,
                       pg_get_expr(policy.polqual, policy.polrelid) AS using_expr,
                       pg_get_expr(policy.polwithcheck, policy.polrelid) AS check_expr,
                       policy.polroles AS roles
                FROM pg_policy AS policy
                JOIN pg_class AS relation ON relation.oid = policy.polrelid
                JOIN pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'public'
                  AND policy.polname = 'submission_definer_access'
                ORDER BY relation.relname
                """
            )
        ).all()
        member_count = connection.scalar(
            text(
                """
                SELECT COUNT(*)
                FROM pg_auth_members AS membership
                JOIN pg_roles AS granted_role
                  ON granted_role.oid = membership.roleid
                WHERE granted_role.rolname IN (
                    'commitmentops_submission',
                    'commitmentops_maintenance'
                )
                """
            )
        )
        migrator_is_member = connection.scalar(
            text(
                """
                SELECT pg_has_role(current_user, 'commitmentops_submission', 'MEMBER')
                """
            )
        )
        public_can_create = connection.scalar(
            text("SELECT has_schema_privilege('public', 'public', 'CREATE')")
        )
        submission_digest = connection.scalar(
            text(
                """
                SELECT has_function_privilege(
                    'commitmentops_submission',
                    'extensions.digest(bytea, text)',
                    'EXECUTE'
                )
                """
            )
        )
        app_can_use_crypto = connection.scalar(
            text(
                """
                SELECT has_schema_privilege(
                    'commitmentops_app', 'extensions', 'USAGE'
                )
                """
            )
        )
        explicit_crypto_grants = connection.scalar(
            text(
                """
                SELECT COUNT(*)
                FROM pg_proc AS routine
                JOIN pg_namespace AS namespace
                  ON namespace.oid = routine.pronamespace
                WHERE namespace.nspname = 'extensions'
                  AND NOT (
                      routine.proname = 'digest'
                      AND pg_get_function_identity_arguments(routine.oid)
                          = 'bytea, text'
                  )
                  AND routine.proacl::text LIKE '%commitmentops_submission%'
                """
            )
        )
        digest_acl = connection.scalar(
            text(
                """
                SELECT routine.proacl::text
                FROM pg_proc AS routine
                JOIN pg_namespace AS namespace
                  ON namespace.oid = routine.pronamespace
                WHERE namespace.nspname = 'extensions'
                  AND routine.proname = 'digest'
                  AND pg_get_function_identity_arguments(routine.oid)
                      = 'bytea, text'
                """
            )
        )
    assert submission_oid is not None
    assert [row.table_name for row in policies] == list(_DEFINER_TABLES)
    for row in policies:
        assert row.row_security is True
        assert row.forced is True
        assert row.command == "*"
        assert row.using_expr == "true"
        assert row.check_expr == "true"
        assert list(row.roles) == [submission_oid]
    assert member_count == 0
    assert migrator_is_member is False
    assert public_can_create is False
    assert submission_digest is True
    assert app_can_use_crypto is False
    assert explicit_crypto_grants == 0
    assert digest_acl is not None
    assert "commitmentops_submission" in digest_acl

    with engine.connect() as connection:
        removed = connection.begin()
        try:
            connection.execute(
                text(
                    "DROP POLICY submission_definer_access ON submission_links"
                )
            )
            hidden = connection.scalar(
                text(
                    """
                    SELECT COUNT(*)
                    FROM public_submission_context(:token)
                    """
                ),
                {"token": token},
            )
            assert hidden == 0
        finally:
            removed.rollback()
        restored = connection.begin()
        try:
            visible = connection.scalar(
                text(
                    """
                    SELECT COUNT(*)
                    FROM public_submission_context(:token)
                    """
                ),
                {"token": token},
            )
            assert visible == 1
            connection.execute(
                text(
                    """
                    UPDATE submission_links
                       SET expires_at = clock_timestamp() - interval '1 minute'
                    """
                )
            )
            expired = connection.scalar(
                text(
                    """
                    SELECT COUNT(*)
                    FROM public_submission_context(:token)
                    """
                ),
                {"token": token},
            )
            assert expired == 0
            connection.execute(
                text(
                    """
                    UPDATE submission_links
                       SET expires_at = clock_timestamp() + interval '1 day',
                           use_count = max_uses
                    """
                )
            )
            exhausted = connection.scalar(
                text(
                    """
                    SELECT COUNT(*)
                    FROM public_submission_context(:token)
                    """
                ),
                {"token": token},
            )
            assert exhausted == 0
        finally:
            restored.rollback()

    _assert_app_cannot_read_other_tenants_or_private_tables(engine)
    _assert_later_revision_can_run_after_membership_is_revoked(engine)


def _assert_app_cannot_read_other_tenants_or_private_tables(engine) -> None:
    token = "clean-migration-token"
    other_token = "other-tenant-token"
    other_organization_id = uuid4()
    other_workflow_id = uuid4()
    other_requirement_id = uuid4()
    other_link_id = uuid4()
    evidence_id = uuid4()
    file_id = uuid4()
    reservation_evidence_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        organization_id = connection.scalar(
            text("SELECT organization_id FROM submission_links LIMIT 1")
        )
        requirement_id = connection.scalar(
            text("SELECT requirement_id FROM submission_links LIMIT 1")
        )
        connection.execute(
            text(
                """
                INSERT INTO organizations (id, name, display_name)
                VALUES (:id, 'Other tenant', 'Other tenant org')
                """
            ),
            {"id": other_organization_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO workflows (id, organization_id, name, workflow_type, status)
                VALUES (:id, :organization_id, 'Other workflow', 'general', 'active')
                """
            ),
            {"id": other_workflow_id, "organization_id": other_organization_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO requirements (
                    id, workflow_id, title, description, owner_id,
                    evidence_expectation, provenance, status, review_status
                )
                SELECT :id, :workflow_id, 'Other requirement', description, owner_id,
                       evidence_expectation, provenance, 'not_started', 'approved'
                FROM requirements
                WHERE id = :source_requirement
                """
            ),
            {
                "id": other_requirement_id,
                "workflow_id": other_workflow_id,
                "source_requirement": requirement_id,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO submission_links (
                    id, organization_id, workflow_id, requirement_id, token_hash,
                    created_at, expires_at, created_by_user_id, max_uses, use_count
                )
                SELECT :id, :organization_id, :workflow_id, :requirement_id,
                       :token_hash, created_at, expires_at, created_by_user_id,
                       max_uses, use_count
                FROM submission_links
                LIMIT 1
                """
            ),
            {
                "id": other_link_id,
                "organization_id": other_organization_id,
                "workflow_id": other_workflow_id,
                "requirement_id": other_requirement_id,
                "token_hash": submission_token_hash(other_token),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO evidence (
                    id, requirement_id, description, evidence_type,
                    submitted_by_id, submitted_at, status, identity_verified
                )
                VALUES (
                    :id, :requirement_id, 'Private evidence', 'note',
                    NULL, :submitted_at, 'submitted', false
                )
                """
            ),
            {
                "id": evidence_id,
                "requirement_id": requirement_id,
                "submitted_at": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO evidence_files (
                    id, evidence_id, organization_id, storage_key,
                    original_filename, media_type, byte_size,
                    checksum_sha256, uploaded_at, scan_status
                )
                VALUES (
                    :id, :evidence_id, :organization_id, :storage_key,
                    'notes.pdf', 'application/pdf', 8,
                    :checksum, :uploaded_at, 'unscanned'
                )
                """
            ),
            {
                "id": file_id,
                "evidence_id": evidence_id,
                "organization_id": organization_id,
                "storage_key": build_storage_key(
                    str(organization_id), str(evidence_id), token_hex(16)
                ),
                "checksum": "a" * 64,
                "uploaded_at": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO evidence_file_reservations (
                    storage_key, evidence_id, organization_id,
                    reserved_at, expires_at
                )
                VALUES (
                    :storage_key, :evidence_id, :organization_id,
                    :reserved_at, :expires_at
                )
                """
            ),
            {
                "storage_key": build_storage_key(
                    str(organization_id),
                    str(reservation_evidence_id),
                    token_hex(16),
                ),
                "evidence_id": evidence_id,
                "organization_id": organization_id,
                "reserved_at": now,
                "expires_at": now + timedelta(hours=1),
            },
        )
        connection.execute(text("SET LOCAL ROLE commitmentops_app"))
        connection.execute(text("SELECT set_config('app.user_id', '', true)"))
        connection.execute(
            text("SELECT set_config('app.organization_id', '', true)")
        )
        own_context = connection.execute(
            text(
                """
                SELECT organization_name, requirement_title
                FROM public_submission_context(:token)
                """
            ),
            {"token": token},
        ).one()
        other_context = connection.execute(
            text(
                """
                SELECT organization_name, requirement_title
                FROM public_submission_context(:token)
                """
            ),
            {"token": other_token},
        ).one()
        unknown = connection.scalar(
            text(
                """
                SELECT COUNT(*)
                FROM public_submission_context(:token)
                """
            ),
            {"token": "not-a-valid-token"},
        )
        assert connection.scalar(text("SELECT COUNT(*) FROM evidence")) == 0
        assert connection.scalar(text("SELECT COUNT(*) FROM evidence_files")) == 0
        assert connection.scalar(text("SELECT COUNT(*) FROM workflows")) == 0
        with pytest.raises(SQLAlchemyError):
            with connection.begin_nested():
                connection.execute(text("SELECT token_hash FROM submission_links"))
        with pytest.raises(SQLAlchemyError):
            with connection.begin_nested():
                connection.execute(
                    text("SELECT storage_key FROM evidence_file_reservations")
                )
        with pytest.raises(SQLAlchemyError):
            with connection.begin_nested():
                connection.execute(
                    text("SELECT storage_key FROM evidence_storage_cleanup")
                )
        with pytest.raises(SQLAlchemyError):
            with connection.begin_nested():
                connection.execute(
                    text("SELECT claim_evidence_storage_key(86400)")
                )
        with pytest.raises(SQLAlchemyError):
            with connection.begin_nested():
                connection.execute(text("SET LOCAL ROLE commitmentops_submission"))
        with pytest.raises(SQLAlchemyError):
            with connection.begin_nested():
                connection.execute(
                    text(
                        """
                        INSERT INTO audit_events (
                            id, occurred_at, entity_type, entity_id, event_type,
                            after_state, actor_reference
                        )
                        VALUES (
                            :id, clock_timestamp(), 'workflow', :entity_id,
                            'forged', CAST('{}' AS json), 'attacker'
                        )
                        """
                    ),
                    {"id": uuid4(), "entity_id": other_workflow_id},
                )
    assert tuple(own_context) == ("Clean migration org", "Clean requirement")
    assert tuple(other_context) == ("Other tenant org", "Other requirement")
    assert unknown == 0


def _assert_later_revision_can_run_after_membership_is_revoked(engine) -> None:
    """A later revision can change schema without keeping the definer role.

    Reassigning a function to that role needs the same temporary membership
    and schema CREATE privilege that revision 08 grants and then revokes.
    Membership alone is not enough. PostgreSQL 15 allows the migration
    login to grant the role again. PostgreSQL 16 does not, unless that
    login already administers the role, so the re-grant proof stops after
    the denied owner change.
    The transaction is rolled back so the probe objects are not kept.
    """

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(
                text("CREATE TABLE public.commitmentops_future_probe (id integer)")
            )
            connection.execute(text("DROP TABLE public.commitmentops_future_probe"))
            connection.execute(
                text(
                    """
                    CREATE FUNCTION public.commitmentops_future_probe()
                    RETURNS void
                    LANGUAGE sql
                    AS 'SELECT NULL'
                    """
                )
            )
            with pytest.raises(SQLAlchemyError):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            """
                            ALTER FUNCTION public.commitmentops_future_probe()
                            OWNER TO commitmentops_submission
                            """
                        )
                    )
            server_version = int(connection.scalar(text("SHOW server_version_num")))
            if server_version >= 160000:
                return
            connection.execute(
                text("GRANT commitmentops_submission TO CURRENT_USER")
            )
            with pytest.raises(SQLAlchemyError):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            """
                            ALTER FUNCTION public.commitmentops_future_probe()
                            OWNER TO commitmentops_submission
                            """
                        )
                    )
            connection.execute(
                text(
                    "GRANT CREATE ON SCHEMA public TO commitmentops_submission"
                )
            )
            connection.execute(
                text(
                    """
                    ALTER FUNCTION public.commitmentops_future_probe()
                    OWNER TO commitmentops_submission
                    """
                )
            )
            connection.execute(
                text(
                    "REVOKE CREATE ON SCHEMA public FROM commitmentops_submission"
                )
            )
            owner = connection.scalar(
                text(
                    """
                    SELECT pg_get_userbyid(proowner)
                    FROM pg_proc
                    JOIN pg_namespace ON pg_namespace.oid = pg_proc.pronamespace
                    WHERE pg_namespace.nspname = 'public'
                      AND proname = 'commitmentops_future_probe'
                    """
                )
            )
            connection.execute(
                text("REVOKE commitmentops_submission FROM CURRENT_USER")
            )
            still_member = connection.scalar(
                text(
                    """
                    SELECT pg_has_role(
                        current_user, 'commitmentops_submission', 'MEMBER'
                    )
                    """
                )
            )
            can_create = connection.scalar(
                text(
                    """
                    SELECT has_schema_privilege(
                        'commitmentops_submission', 'public', 'CREATE'
                    )
                    """
                )
            )
            assert owner == "commitmentops_submission"
            assert still_member is False
            assert can_create is False
        finally:
            transaction.rollback()


def _migration_config(url: str, runtime: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    config.attributes[RUNTIME_LOGIN_ATTRIBUTE] = runtime
    return config


def _url_for(
    admin_url: str,
    database: str,
    username: str | None,
    password: str | None = None,
) -> str:
    if username is None:
        if password is not None:
            raise RuntimeError("refusing a password without a test login")
        return make_url(admin_url).set(database=database).render_as_string(
            hide_password=False
        )
    if password is None:
        raise RuntimeError("refusing a test login without a password")
    return temporary_role_url(
        admin_url, username, password, database=database
    )


def _engine(url: str):
    return create_engine(
        url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"connect_timeout": 5},
    )


def _drop_clean_database(admin, database: str, migrator: str, runtime: str) -> None:
    try:
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
    finally:
        with admin.connect() as connection:
            for member in (runtime, migrator):
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
            text(
                f"REVOKE {_known_role(granted)} FROM {_ident(member)} CASCADE"
            )
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
