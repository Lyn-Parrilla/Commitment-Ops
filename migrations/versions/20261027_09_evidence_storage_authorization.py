"""Authorize evidence storage cleanup from the reservation, not the key.

Revision ID: 20261027_09
Revises: 20261023_08
Create Date: 2026-10-27
"""

from uuid import UUID

from alembic import op
import sqlalchemy as sa

revision = "20261027_09"
down_revision = "20261023_08"
branch_labels = None
depends_on = None

_FILES = "evidence_files"
_RESERVATIONS = "evidence_file_reservations"
_CLEANUP = "evidence_storage_cleanup"
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
_APP_ROLE = "commitmentops_app"
_SUBMISSION_ROLE = "commitmentops_submission"
_REPLACED = (
    ("record_evidence_storage_failure", "text", "text, text"),
    ("release_evidence_object_reservation", "text", "text, text"),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    quoted = _quote(_current_schema(bind))
    digest_schema = _quote(_digest_schema(bind))
    _replace_functions(
        bind,
        quoted,
        _record_function(quoted, digest_schema),
        _release_function(quoted, digest_schema),
        drop_signature=1,
        create_signature=2,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not op.get_context().as_sql:
        populated = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {_FILES}")
        ).scalar()
        if populated:
            raise RuntimeError(_POPULATED)
        _refuse_populated_tenant_downgrade(bind)
    if bind.dialect.name != "postgresql":
        return
    quoted = _quote(_current_schema(bind))
    _replace_functions(
        bind,
        quoted,
        _previous_record_function(quoted),
        _previous_release_function(quoted),
        drop_signature=2,
        create_signature=1,
    )


def _replace_functions(
    bind: sa.Connection,
    quoted: str,
    record_sql: str,
    release_sql: str,
    *,
    drop_signature: int,
    create_signature: int,
) -> None:
    app = _quote(_APP_ROLE)
    submission = _quote(_SUBMISSION_ROLE)
    for name, previous, current in _REPLACED:
        signature = previous if drop_signature == 1 else current
        bind.execute(
            sa.text(f"DROP FUNCTION IF EXISTS {quoted}.{name}({signature})")
        )
    for statement in (record_sql, release_sql):
        bind.execute(sa.text(statement))
    bind.execute(sa.text(f"GRANT {_SUBMISSION_ROLE} TO CURRENT_USER"))
    bind.execute(sa.text(f"GRANT CREATE ON SCHEMA {quoted} TO {submission}"))
    for name, previous, current in _REPLACED:
        signature = current if create_signature == 2 else previous
        qualified = f"{quoted}.{name}({signature})"
        bind.execute(
            sa.text(
                f"""
                REVOKE ALL ON FUNCTION {qualified} FROM PUBLIC;
                GRANT EXECUTE ON FUNCTION {qualified} TO {app};
                ALTER FUNCTION {qualified} OWNER TO {submission};
                REVOKE ALL ON FUNCTION {qualified} FROM PUBLIC;
                GRANT EXECUTE ON FUNCTION {qualified} TO {app};
                """
            )
        )
    bind.execute(sa.text(f"REVOKE CREATE ON SCHEMA {quoted} FROM {submission}"))
    bind.execute(sa.text(_release_membership_sql()))


def _record_function(quoted: str, digest_schema: str) -> str:
    presented = _token_hash(digest_schema)
    return f"""
    CREATE FUNCTION {quoted}.record_evidence_storage_failure(
        p_token text,
        p_storage_key text
    )
    RETURNS void
    LANGUAGE plpgsql
    VOLATILE
    SECURITY DEFINER
    SET search_path = pg_catalog
    AS $record$
    DECLARE
        v_organization_id uuid;
    BEGIN
        SELECT links.organization_id
          INTO v_organization_id
          FROM {quoted}.submission_links AS links
          JOIN {quoted}.requirements AS requirements
            ON requirements.id = links.requirement_id
           AND requirements.workflow_id = links.workflow_id
          JOIN {quoted}.workflows AS workflows
            ON workflows.id = links.workflow_id
           AND workflows.organization_id = links.organization_id
         WHERE links.token_hash = {presented}
           AND links.revoked_at IS NULL
           AND links.expires_at > pg_catalog.clock_timestamp()
           AND links.use_count < links.max_uses
           AND requirements.status <> 'cancelled'
           AND workflows.status IN ('draft', 'active');
        IF NOT FOUND
           OR p_storage_key IS NULL
           OR p_storage_key !~ '{_KEY_PATTERN}'
           OR pg_catalog.split_part(p_storage_key, '/', 1)
              <> v_organization_id::text THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended(p_storage_key, 0::bigint)
        );
        PERFORM 1
          FROM {quoted}.{_RESERVATIONS} AS reservations
         WHERE reservations.storage_key = p_storage_key
           AND reservations.organization_id = v_organization_id
           AND pg_catalog.split_part(reservations.storage_key, '/', 1)
               = reservations.organization_id::text
           AND pg_catalog.split_part(reservations.storage_key, '/', 2)
               = reservations.evidence_id::text;
        IF NOT FOUND OR EXISTS (
            SELECT 1
              FROM {quoted}.{_FILES} AS files
             WHERE files.storage_key = p_storage_key
        ) THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;
        INSERT INTO {quoted}.{_CLEANUP} (
            storage_key, organization_id, recorded_at
        )
        VALUES (
            p_storage_key, v_organization_id, pg_catalog.clock_timestamp()
        )
        ON CONFLICT (storage_key) DO NOTHING;
    END
    $record$
    """


def _release_function(quoted: str, digest_schema: str) -> str:
    presented = _token_hash(digest_schema)
    return f"""
    CREATE FUNCTION {quoted}.release_evidence_object_reservation(
        p_token text,
        p_storage_key text
    )
    RETURNS void
    LANGUAGE plpgsql
    VOLATILE
    SECURITY DEFINER
    SET search_path = pg_catalog
    AS $release$
    DECLARE
        v_organization_id uuid;
        v_deleted integer;
    BEGIN
        SELECT links.organization_id
          INTO v_organization_id
          FROM {quoted}.submission_links AS links
          JOIN {quoted}.requirements AS requirements
            ON requirements.id = links.requirement_id
           AND requirements.workflow_id = links.workflow_id
          JOIN {quoted}.workflows AS workflows
            ON workflows.id = links.workflow_id
           AND workflows.organization_id = links.organization_id
         WHERE links.token_hash = {presented}
           AND links.revoked_at IS NULL
           AND links.expires_at > pg_catalog.clock_timestamp()
           AND links.use_count < links.max_uses
           AND requirements.status <> 'cancelled'
           AND workflows.status IN ('draft', 'active');
        IF NOT FOUND
           OR p_storage_key IS NULL
           OR p_storage_key !~ '{_KEY_PATTERN}'
           OR pg_catalog.split_part(p_storage_key, '/', 1)
              <> v_organization_id::text THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended(p_storage_key, 0::bigint)
        );
        IF EXISTS (
            SELECT 1
              FROM {quoted}.{_FILES} AS files
             WHERE files.storage_key = p_storage_key
        ) THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;
        DELETE FROM {quoted}.{_RESERVATIONS} AS reservations
         WHERE reservations.storage_key = p_storage_key
           AND reservations.organization_id = v_organization_id
           AND pg_catalog.split_part(reservations.storage_key, '/', 1)
               = reservations.organization_id::text
           AND pg_catalog.split_part(reservations.storage_key, '/', 2)
               = reservations.evidence_id::text;
        GET DIAGNOSTICS v_deleted = ROW_COUNT;
        IF v_deleted <> 1 THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;
    END
    $release$
    """


def _previous_record_function(quoted: str) -> str:
    return f"""
    CREATE FUNCTION {quoted}.record_evidence_storage_failure(p_storage_key text)
    RETURNS void
    LANGUAGE plpgsql
    VOLATILE
    SECURITY DEFINER
    SET search_path = pg_catalog
    AS $record$
    DECLARE
        v_organization_id uuid;
    BEGIN
        IF p_storage_key IS NULL OR p_storage_key !~ '{_KEY_PATTERN}' THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended(p_storage_key, 0::bigint)
        );
        IF EXISTS (
            SELECT 1 FROM {quoted}.{_FILES} AS files
             WHERE files.storage_key = p_storage_key
        ) THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;
        v_organization_id := pg_catalog.split_part(p_storage_key, '/', 1)::uuid;
        INSERT INTO {quoted}.{_CLEANUP} (storage_key, organization_id, recorded_at)
        VALUES (p_storage_key, v_organization_id, pg_catalog.clock_timestamp())
        ON CONFLICT (storage_key) DO NOTHING;
    END
    $record$
    """


def _previous_release_function(quoted: str) -> str:
    return f"""
    CREATE FUNCTION {quoted}.release_evidence_object_reservation(p_storage_key text)
    RETURNS void
    LANGUAGE plpgsql
    VOLATILE
    SECURITY DEFINER
    SET search_path = pg_catalog
    AS $release$
    BEGIN
        IF p_storage_key IS NULL OR p_storage_key !~ '{_KEY_PATTERN}' THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended(p_storage_key, 0::bigint)
        );
        IF EXISTS (
            SELECT 1 FROM {quoted}.{_FILES} AS files
             WHERE files.storage_key = p_storage_key
        ) THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;
        DELETE FROM {quoted}.{_RESERVATIONS}
         WHERE storage_key = p_storage_key;
    END
    $release$
    """


def _token_hash(digest_schema: str) -> str:
    return (
        "CASE\n"
        "            WHEN p_token IS NOT NULL\n"
        "             AND pg_catalog.char_length(p_token) BETWEEN 1 AND 200\n"
        "            THEN pg_catalog.encode(\n"
        f"                {digest_schema}.digest(\n"
        "                    pg_catalog.convert_to(p_token, 'UTF8'), 'sha256'\n"
        "                ),\n"
        "                'hex'\n"
        "            )\n"
        "            ELSE NULL\n"
        "        END"
    )


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


def _refuse_populated_tenant_downgrade(bind: sa.Connection) -> None:
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


def _digest_schema(bind: sa.Connection) -> str:
    if op.get_context().as_sql:
        return "commitmentops_crypto"
    schema = bind.execute(
        sa.text(
            """
            SELECT namespace.nspname
            FROM pg_extension AS extension
            JOIN pg_namespace AS namespace
              ON namespace.oid = extension.extnamespace
            WHERE extension.extname = 'pgcrypto'
            """
        )
    ).scalar()
    if not isinstance(schema, str):
        raise RuntimeError("pgcrypto is not installed")
    return _validated_identifier(schema)


def _current_schema(bind: sa.Connection) -> str:
    if op.get_context().as_sql:
        return "public"
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    if not isinstance(schema, str) or schema.lower() in _DENIED_SCHEMAS:
        raise RuntimeError("refusing to authorize evidence storage in this schema")
    return _validated_identifier(schema)


def _validated_identifier(identifier: str) -> str:
    if not identifier or not identifier.replace("_", "").isalnum():
        raise RuntimeError("refusing an unexpected schema name")
    if identifier[0].isdigit():
        raise RuntimeError("refusing an unexpected schema name")
    return identifier


def _quote(identifier: str) -> str:
    return '"' + _validated_identifier(identifier).replace('"', "") + '"'
