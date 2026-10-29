"""Revoke Supabase API-role access to evidence and migration metadata.

Revision ID: 20261028_10
Revises: 20261027_09
Create Date: 2026-10-28

Supabase grants EXECUTE on new public functions to ``anon``,
``authenticated``, and ``service_role``. Revoking ``PUBLIC`` leaves those
grants in place. This revision removes them from the evidence functions and
from ``public.alembic_version``. It does not grant those roles anything on
the way down.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from alembic import op
import sqlalchemy as sa

revision = "20261028_10"
down_revision = "20261027_09"
branch_labels = None
depends_on = None

_PREDECESSOR = "20261027_09"
_FILES = "evidence_files"
_POPULATED = (
    "refusing to downgrade while evidence files exist. "
    "Remove those private files through the application first."
)
_LEGACY_ORGANIZATION_ID = UUID("00000000-0000-0000-0000-000000000001")
_TENANT_POPULATED = (
    "Refusing to downgrade while non-legacy organizations, "
    "organization memberships, or non-legacy workflow assignments "
    "exist. Export or deliberately remove those tenant records "
    "before downgrading."
)
_APP_ROLE = "commitmentops_app"
_SUBMISSION_ROLE = "commitmentops_submission"
_MAINTENANCE_ROLE = "commitmentops_maintenance"
_API_ROLES = ("anon", "authenticated", "service_role")
_APP_FUNCTIONS = (
    ("record_evidence_storage_failure", "text, text"),
    ("release_evidence_object_reservation", "text, text"),
    ("reserve_evidence_object", "text, uuid, text"),
    (
        "submit_public_evidence_with_file",
        "text, text, text, text, uuid, text, text, text, integer, text",
    ),
)
_MAINTENANCE_FUNCTIONS = (
    ("claim_evidence_storage_key", "integer"),
    ("complete_evidence_storage_cleanup", "text, uuid"),
    ("confirm_evidence_storage_claim", "text, uuid"),
)
_KEY_PATTERN = (
    "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    "/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    "/[0-9a-f]{32}$"
)
_DENIED_SCHEMAS = frozenset(
    {
        "auth",
        "storage",
        "extensions",
        "graphql",
        "graphql_public",
        "realtime",
        "supabase_migrations",
        "information_schema",
        "pg_catalog",
        "pg_toast",
        "vault",
        "commitmentops_crypto",
    }
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    quoted = _quote(_current_schema(bind))
    with _submission_owner(bind, quoted):
        _install_confirm_function(bind, quoted)
        for name, signature in _APP_FUNCTIONS:
            _restrict_function(bind, quoted, name, signature, _APP_ROLE)
        for name, signature in _MAINTENANCE_FUNCTIONS:
            _restrict_function(bind, quoted, name, signature, _MAINTENANCE_ROLE)
    _harden_alembic_version(bind)


def downgrade() -> None:
    """Keep the function revokes. Do not restore API-role access.

    Row-level security on ``public.alembic_version`` is disabled when that
    table exists. No privileges are granted to ``anon``, ``authenticated``,
    or ``service_role``.

    SQLite commits each revision. This revision's downgrade changes no
    schema there, so a later refusal would leave the stamp at
    ``20261027_09`` after a failed command. Refuse first, using revision
    09's checks, when that revision will run in the same command.
    """

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        if not op.get_context().as_sql and _downgrade_includes(_PREDECESSOR):
            _refuse_blocked_downgrade(bind)
        return
    quoted = _quote(_current_schema(bind))
    with _submission_owner(bind, quoted):
        bind.execute(
            sa.text(
                "DROP FUNCTION IF EXISTS "
                f"{quoted}.confirm_evidence_storage_claim(text, uuid)"
            )
        )
        for name, signature in _APP_FUNCTIONS:
            _restrict_function(bind, quoted, name, signature, _APP_ROLE)
        for name, signature in _MAINTENANCE_FUNCTIONS[:2]:
            _restrict_function(bind, quoted, name, signature, _MAINTENANCE_ROLE)
    _disable_alembic_version_rls(bind)


@contextmanager
def _submission_owner(bind: sa.Connection, quoted: str) -> Iterator[None]:
    """Borrow the definer role only while changing its functions."""

    submission = _quote(_SUBMISSION_ROLE)
    bind.execute(sa.text(f"GRANT {_SUBMISSION_ROLE} TO CURRENT_USER"))
    bind.execute(sa.text(f"GRANT CREATE ON SCHEMA {quoted} TO {submission}"))
    try:
        yield
    finally:
        bind.execute(sa.text(f"REVOKE CREATE ON SCHEMA {quoted} FROM {submission}"))
        bind.execute(sa.text(_release_membership_sql()))


def _install_confirm_function(bind: sa.Connection, quoted: str) -> None:
    submission = _quote(_SUBMISSION_ROLE)
    bind.execute(sa.text(_confirm_function_sql(quoted)))
    qualified = f"{quoted}.confirm_evidence_storage_claim(text, uuid)"
    bind.execute(
        sa.text(
            f"""
            REVOKE ALL ON FUNCTION {qualified} FROM PUBLIC;
            ALTER FUNCTION {qualified} OWNER TO {submission};
            REVOKE ALL ON FUNCTION {qualified} FROM PUBLIC;
            """
        )
    )


def _restrict_function(
    bind: sa.Connection,
    quoted: str,
    name: str,
    signature: str,
    grantee: str,
) -> None:
    qualified = f"{quoted}.{name}({signature})"
    bind.execute(sa.text(f"REVOKE ALL ON FUNCTION {qualified} FROM PUBLIC"))
    for role in _API_ROLES:
        _revoke_if_role_exists(
            bind,
            f"REVOKE ALL ON FUNCTION {qualified} FROM {_quote(role)}",
            role,
        )
    bind.execute(
        sa.text(f"GRANT EXECUTE ON FUNCTION {qualified} TO {_quote(grantee)}")
    )


def _harden_alembic_version(bind: sa.Connection) -> None:
    revokes = "\n".join(
        f"""
                IF EXISTS (
                    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{role}'
                ) THEN
                    EXECUTE 'REVOKE ALL ON TABLE public.alembic_version '
                        'FROM {_quote(role)}';
                END IF;
        """
        for role in _API_ROLES
    )
    bind.execute(
        sa.text(
            f"""
            DO $alembic_rls$
            BEGIN
                IF to_regclass('public.alembic_version') IS NULL THEN
                    RETURN;
                END IF;
                {revokes}
                EXECUTE
                    'ALTER TABLE public.alembic_version '
                    'ENABLE ROW LEVEL SECURITY';
            END
            $alembic_rls$
            """
        )
    )


def _disable_alembic_version_rls(bind: sa.Connection) -> None:
    bind.execute(
        sa.text(
            """
            DO $alembic_rls$
            BEGIN
                IF to_regclass('public.alembic_version') IS NOT NULL THEN
                    EXECUTE
                        'ALTER TABLE public.alembic_version '
                        'DISABLE ROW LEVEL SECURITY';
                END IF;
            END
            $alembic_rls$
            """
        )
    )


def _revoke_if_role_exists(bind: sa.Connection, statement: str, role: str) -> None:
    if role not in _API_ROLES:
        raise RuntimeError("refusing an unexpected role name")
    escaped = statement.replace("'", "''")
    bind.execute(
        sa.text(
            f"""
            DO $revoke_api_role$
            BEGIN
                IF EXISTS (
                    SELECT 1
                      FROM pg_catalog.pg_roles
                     WHERE rolname = '{role}'
                ) THEN
                    EXECUTE '{escaped}';
                END IF;
            END
            $revoke_api_role$
            """
        )
    )


def _confirm_function_sql(quoted: str) -> str:
    return f"""
    CREATE FUNCTION {quoted}.confirm_evidence_storage_claim(
        p_storage_key text,
        p_claim_token uuid
    )
    RETURNS boolean
    LANGUAGE plpgsql
    VOLATILE
    SECURITY DEFINER
    SET search_path = pg_catalog
    AS $confirm$
    BEGIN
        IF p_storage_key IS NULL
           OR p_claim_token IS NULL
           OR p_storage_key !~ '{_KEY_PATTERN}' THEN
            RETURN false;
        END IF;
        RETURN EXISTS (
            SELECT 1
              FROM {quoted}.evidence_storage_cleanup AS cleanup
             WHERE cleanup.storage_key = p_storage_key
               AND cleanup.claim_token = p_claim_token
               AND NOT EXISTS (
                    SELECT 1
                      FROM {quoted}.evidence_files AS files
                     WHERE files.storage_key = cleanup.storage_key
               )
               AND NOT EXISTS (
                    SELECT 1
                      FROM {quoted}.evidence_file_reservations AS reservations
                     WHERE reservations.storage_key = cleanup.storage_key
                       AND reservations.expires_at > pg_catalog.clock_timestamp()
               )
        );
    END
    $confirm$
    """


def _downgrade_includes(revision: str) -> bool:
    """Return whether this command will run another revision's downgrade."""

    context = op.get_context()
    environment = context.environment_context
    if environment is None or environment.script is None:
        return False
    destination = environment.context_opts.get("destination_rev")
    if not isinstance(destination, str):
        return False
    heads = context.get_current_heads()
    if not heads:
        return False
    steps = environment.script._downgrade_revs(destination, heads)
    return any(
        getattr(getattr(step, "revision", None), "revision", None) == revision
        for step in steps
    )


def _refuse_blocked_downgrade(bind: sa.Connection) -> None:
    """Match revision 09's refusal so a failed command does not move the stamp."""

    populated = bind.execute(sa.text(f"SELECT COUNT(*) FROM {_FILES}")).scalar()
    if populated:
        raise RuntimeError(_POPULATED)
    organizations = sa.table("organizations", sa.column("id", sa.Uuid()))
    workflows = sa.table("workflows", sa.column("organization_id", sa.Uuid()))
    memberships = sa.table(
        "organization_memberships",
        sa.column("organization_id", sa.Uuid()),
    )
    nonlegacy_organizations = bind.scalar(
        sa.select(sa.func.count())
        .select_from(organizations)
        .where(organizations.c.id != _LEGACY_ORGANIZATION_ID)
    )
    assigned_workflows = bind.scalar(
        sa.select(sa.func.count())
        .select_from(workflows)
        .where(workflows.c.organization_id != _LEGACY_ORGANIZATION_ID)
    )
    membership_count = bind.scalar(
        sa.select(sa.func.count()).select_from(memberships)
    )
    if (
        nonlegacy_organizations is None
        or assigned_workflows is None
        or membership_count is None
        or nonlegacy_organizations
        or assigned_workflows
        or membership_count
    ):
        raise RuntimeError(_TENANT_POPULATED)


def _release_membership_sql() -> str:
    return """
    DO $revoke_membership$
    BEGIN
        IF EXISTS (
            SELECT 1
              FROM pg_auth_members AS membership
              JOIN pg_roles AS granted_role
                ON granted_role.oid = membership.roleid
              JOIN pg_roles AS member_role
                ON member_role.oid = membership.member
             WHERE granted_role.rolname = 'commitmentops_submission'
               AND member_role.rolname = current_user
        ) THEN
            REVOKE commitmentops_submission FROM CURRENT_USER;
        END IF;
    END
    $revoke_membership$
    """


def _current_schema(bind: sa.Connection) -> str:
    if op.get_context().as_sql:
        return "public"
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    if not isinstance(schema, str) or schema.lower() in _DENIED_SCHEMAS:
        raise RuntimeError("refusing to harden privileges in this schema")
    return _validated_identifier(schema)


def _validated_identifier(identifier: str) -> str:
    if not identifier or not identifier.replace("_", "").isalnum():
        raise RuntimeError("refusing an unexpected schema name")
    if identifier[0].isdigit():
        raise RuntimeError("refusing an unexpected schema name")
    return identifier


def _quote(identifier: str) -> str:
    return '"' + _validated_identifier(identifier).replace('"', "") + '"'
