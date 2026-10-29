"""Private evidence-file metadata. Bytes stay outside the database.

Revision ID: 20261023_08
Revises: 20261022_07
"""

from __future__ import annotations

from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision = "20261023_08"
down_revision = "20261022_07"
branch_labels = None
depends_on = None

FILES = "evidence_files"
RESERVATIONS = "evidence_file_reservations"
CLEANUP = "evidence_storage_cleanup"
POPULATED = (
    "refusing to downgrade while evidence files exist. "
    "Remove those private files through the application first."
)
LEGACY_ORGANIZATION_ID = UUID("00000000-0000-0000-0000-000000000001")
TENANT_POPULATED = (
    "Refusing to downgrade while non-legacy organizations, "
    "organization memberships, or non-legacy workflow assignments "
    "exist. Export or deliberately remove those tenant records "
    "before downgrading."
)
_KEY_CHECK = (
    "length(storage_key) = 106 "
    "AND storage_key NOT LIKE '%..%' "
    "AND storage_key NOT LIKE '/%'"
)
_KEY_PATTERN = (
    "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/"
    "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/"
    "[0-9a-f]{32}$"
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
    _create_tables()
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    schema = _current_schema(bind)
    quoted = _quote(schema)
    digest_schema = _digest_schema(bind)
    _install_boundary(bind, schema, quoted, digest_schema)


def downgrade() -> None:
    bind = op.get_bind()
    if not op.get_context().as_sql:
        populated = bind.execute(sa.text(f"SELECT COUNT(*) FROM {FILES}")).scalar()
        if populated:
            raise RuntimeError(POPULATED)
        _refuse_populated_tenant_downgrade(bind)
    if bind.dialect.name == "postgresql":
        quoted = _quote(_current_schema(bind))
        _drop_boundary(bind, quoted)
    op.drop_table(CLEANUP)
    op.drop_table(RESERVATIONS)
    op.drop_table(FILES)


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
        .where(organizations.c.id != LEGACY_ORGANIZATION_ID)
    )
    assigned_workflows = bind.scalar(
        sa.select(sa.func.count())
        .select_from(workflows)
        .where(workflows.c.organization_id != LEGACY_ORGANIZATION_ID)
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
        raise RuntimeError(TENANT_POPULATED)


def _create_tables() -> None:
    op.create_table(
        FILES,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("storage_key", sa.String(length=200), nullable=False),
        sa.Column("original_filename", sa.String(length=200), nullable=False),
        sa.Column("media_type", sa.String(length=120), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "scan_status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'unscanned'"),
        ),
        sa.CheckConstraint(
            "byte_size >= 1 AND byte_size <= 10485760",
            name="ck_evidence_files_byte_size",
        ),
        sa.CheckConstraint(
            "length(checksum_sha256) = 64",
            name="ck_evidence_files_checksum",
        ),
        sa.CheckConstraint(
            "scan_status = 'unscanned'",
            name="ck_evidence_files_scan_status",
        ),
        sa.CheckConstraint(
            "length(original_filename) BETWEEN 1 AND 200",
            name="ck_evidence_files_filename",
        ),
        sa.CheckConstraint(_KEY_CHECK, name="ck_evidence_files_storage_key"),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["evidence.id"],
            name="fk_evidence_files_evidence_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_evidence_files_organization_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("evidence_id", name="uq_evidence_files_evidence_id"),
        sa.UniqueConstraint("storage_key", name="uq_evidence_files_storage_key"),
    )
    op.create_index(
        "ix_evidence_files_organization",
        FILES,
        ["organization_id"],
    )
    op.create_table(
        RESERVATIONS,
        sa.Column("storage_key", sa.String(length=200), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            _KEY_CHECK,
            name="ck_evidence_file_reservations_storage_key",
        ),
        sa.PrimaryKeyConstraint("storage_key"),
    )
    op.create_table(
        CLEANUP,
        sa.Column("storage_key", sa.String(length=200), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            _KEY_CHECK,
            name="ck_evidence_storage_cleanup_storage_key",
        ),
        sa.PrimaryKeyConstraint("storage_key"),
    )


def _install_boundary(
    bind: sa.Connection,
    schema: str,
    quoted: str,
    digest_schema: str,
) -> None:
    app = _quote("commitmentops_app")
    submission = _quote("commitmentops_submission")
    maintenance = _quote("commitmentops_maintenance")
    _install_maintenance_role(bind)
    bind.execute(sa.text(f"GRANT USAGE ON SCHEMA {quoted} TO {maintenance}"))
    bind.execute(
        sa.text(
            f"""
            REVOKE ALL ON TABLE {quoted}.{FILES} FROM PUBLIC;
            REVOKE ALL ON TABLE {quoted}.{RESERVATIONS} FROM PUBLIC;
            REVOKE ALL ON TABLE {quoted}.{CLEANUP} FROM PUBLIC;
            GRANT SELECT (
                id, evidence_id, organization_id, original_filename,
                media_type, byte_size, checksum_sha256, uploaded_at, scan_status
            ) ON TABLE {quoted}.{FILES} TO {app};
            GRANT SELECT, INSERT ON TABLE {quoted}.{FILES} TO {submission};
            GRANT SELECT, INSERT, DELETE ON TABLE {quoted}.{RESERVATIONS} TO {submission};
            GRANT SELECT, INSERT, DELETE ON TABLE {quoted}.{CLEANUP} TO {submission};
            GRANT UPDATE (claimed_at, claim_token)
                ON TABLE {quoted}.{CLEANUP} TO {submission};
            ALTER TABLE {quoted}.{FILES} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {quoted}.{FILES} FORCE ROW LEVEL SECURITY;
            ALTER TABLE {quoted}.{RESERVATIONS} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {quoted}.{RESERVATIONS} FORCE ROW LEVEL SECURITY;
            ALTER TABLE {quoted}.{CLEANUP} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {quoted}.{CLEANUP} FORCE ROW LEVEL SECURITY;
            """
        )
    )
    for table in (FILES, RESERVATIONS, CLEANUP):
        bind.execute(
            sa.text(
                f"""
                CREATE POLICY submission_definer_access
                ON {quoted}.{table}
                FOR ALL
                TO {submission}
                USING (true)
                WITH CHECK (true)
                """
            )
        )
    bind.execute(
        sa.text(
            f"""
            CREATE POLICY evidence_files_tenant
            ON {quoted}.{FILES}
            FOR SELECT
            USING (
                EXISTS (
                    SELECT 1
                      FROM {quoted}.evidence AS evidence
                      JOIN {quoted}.requirements AS requirements
                        ON requirements.id = evidence.requirement_id
                      JOIN {quoted}.workflows AS workflows
                        ON workflows.id = requirements.workflow_id
                     WHERE evidence.id = {quoted}.{FILES}.evidence_id
                       AND {quoted}.{FILES}.organization_id = workflows.organization_id
                       AND workflows.organization_id = {quoted}.app_current_organization_id()
                       AND {quoted}.app_is_active_member(workflows.organization_id)
                )
            )
            """
        )
    )
    for statement in (
        _target_function(quoted, digest_schema),
        _reserve_function(quoted, digest_schema),
        _submit_file_function(quoted, digest_schema),
        _record_function(quoted),
        _release_function(quoted),
        _claim_function(quoted),
        _complete_cleanup_function(quoted),
    ):
        bind.execute(sa.text(statement))
    bind.execute(sa.text("GRANT commitmentops_submission TO CURRENT_USER"))
    bind.execute(sa.text(f"GRANT CREATE ON SCHEMA {quoted} TO {submission}"))
    for name, signature in _APP_FUNCTIONS:
        _grant_function(bind, quoted, name, signature, app, submission)
    for name, signature in _MAINTENANCE_FUNCTIONS:
        _grant_function(bind, quoted, name, signature, maintenance, submission)
    bind.execute(sa.text(f"REVOKE CREATE ON SCHEMA {quoted} FROM {submission}"))
    bind.execute(
        sa.text(
            """
            DO $release$
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
                IF EXISTS (
                    SELECT 1
                      FROM pg_auth_members AS membership
                      JOIN pg_roles AS granted_role
                        ON granted_role.oid = membership.roleid
                      JOIN pg_roles AS member_role
                        ON member_role.oid = membership.member
                     WHERE granted_role.rolname = 'commitmentops_maintenance'
                       AND member_role.rolname = current_user
                ) THEN
                    REVOKE commitmentops_maintenance FROM CURRENT_USER;
                END IF;
            END
            $release$
            """
        )
    )


def _drop_boundary(bind: sa.Connection, quoted: str) -> None:
    app = _quote("commitmentops_app")
    maintenance = _quote("commitmentops_maintenance")
    for name, signature in reversed(_FUNCTION_SIGNATURES):
        bind.execute(
            sa.text(f"DROP FUNCTION IF EXISTS {quoted}.{name}({signature})")
        )
    bind.execute(
        sa.text(
            f"""
            DROP POLICY IF EXISTS evidence_files_tenant ON {quoted}.{FILES};
            REVOKE ALL ON TABLE {quoted}.{FILES} FROM {app};
            REVOKE USAGE ON SCHEMA {quoted} FROM {maintenance};
            """
        )
    )


def _grant_function(
    bind: sa.Connection,
    quoted: str,
    name: str,
    signature: str,
    grantee: str,
    owner: str,
) -> None:
    qualified = f"{quoted}.{name}({signature})"
    bind.execute(
        sa.text(
            f"""
            REVOKE ALL ON FUNCTION {qualified} FROM PUBLIC;
            GRANT EXECUTE ON FUNCTION {qualified} TO {grantee};
            ALTER FUNCTION {qualified} OWNER TO {owner};
            """
        )
    )


def _install_maintenance_role(bind: sa.Connection) -> None:
    bind.execute(sa.text(_MAINTENANCE_ROLE_STATEMENT))


_MAINTENANCE_ROLE_STATEMENT = """
DO $maintenance$
DECLARE
    existing record;
BEGIN
    SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
      INTO existing
      FROM pg_roles
     WHERE rolname = 'commitmentops_maintenance';
    IF NOT FOUND THEN
        CREATE ROLE commitmentops_maintenance
            NOLOGIN
            NOSUPERUSER
            NOBYPASSRLS
            NOCREATEDB
            NOCREATEROLE;
        SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
          INTO existing
          FROM pg_roles
         WHERE rolname = 'commitmentops_maintenance';
    END IF;
    IF existing.rolcanlogin
       OR existing.rolsuper
       OR existing.rolbypassrls
       OR existing.rolcreatedb
       OR existing.rolcreaterole THEN
        RAISE EXCEPTION
            'Refusing to use existing role commitmentops_maintenance because it is not NOLOGIN, NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, and NOCREATEROLE';
    END IF;
END
$maintenance$
"""


_FUNCTION_SIGNATURES = (
    ("submission_target_organization", "text"),
    ("reserve_evidence_object", "text, uuid, text"),
    (
        "submit_public_evidence_with_file",
        "text, text, text, text, uuid, text, text, text, integer, text",
    ),
    ("record_evidence_storage_failure", "text"),
    ("release_evidence_object_reservation", "text"),
    ("claim_evidence_storage_key", "integer"),
    ("complete_evidence_storage_cleanup", "text, uuid"),
)
_APP_FUNCTIONS = _FUNCTION_SIGNATURES[:5]
_MAINTENANCE_FUNCTIONS = _FUNCTION_SIGNATURES[5:]


def _token_hash(digest_schema: str) -> str:
    return (
        f"CASE WHEN p_token IS NULL "
        f"OR pg_catalog.length(p_token) < 1 "
        f"OR pg_catalog.length(p_token) > 200 "
        f"THEN NULL "
        f"ELSE encode("
        f"{_quote(digest_schema)}.digest("
        f"pg_catalog.convert_to(p_token, 'UTF8'), 'sha256'"
        f"), 'hex') END"
    )


def _target_function(quoted: str, digest_schema: str) -> str:
    presented = _token_hash(digest_schema)
    return f"""
    CREATE FUNCTION {quoted}.submission_target_organization(p_token text)
    RETURNS uuid
    LANGUAGE plpgsql
    STABLE
    SECURITY DEFINER
    SET search_path = pg_catalog
    AS $target$
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
        RETURN v_organization_id;
    END
    $target$
    """


def _reserve_function(quoted: str, digest_schema: str) -> str:
    presented = _token_hash(digest_schema)
    return f"""
    CREATE FUNCTION {quoted}.reserve_evidence_object(
        p_token text,
        p_evidence_id uuid,
        p_storage_key text
    )
    RETURNS uuid
    LANGUAGE plpgsql
    VOLATILE
    SECURITY DEFINER
    SET search_path = pg_catalog
    AS $reserve$
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
        IF NOT FOUND OR p_evidence_id IS NULL OR p_storage_key IS NULL THEN
            RETURN NULL;
        END IF;
        IF p_storage_key !~ '{_KEY_PATTERN}'
           OR pg_catalog.split_part(p_storage_key, '/', 1) <> v_organization_id::text
           OR pg_catalog.split_part(p_storage_key, '/', 2) <> p_evidence_id::text THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended(p_storage_key, 0::bigint)
        );
        IF EXISTS (
            SELECT 1
              FROM {quoted}.{FILES} AS files
             WHERE files.storage_key = p_storage_key
        ) OR EXISTS (
            SELECT 1
              FROM {quoted}.{CLEANUP} AS cleanup
             WHERE cleanup.storage_key = p_storage_key
        ) THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;
        INSERT INTO {quoted}.{RESERVATIONS} (
            storage_key, evidence_id, organization_id, reserved_at, expires_at
        )
        VALUES (
            p_storage_key,
            p_evidence_id,
            v_organization_id,
            pg_catalog.clock_timestamp(),
            pg_catalog.clock_timestamp() + interval '1 hour'
        );
        RETURN v_organization_id;
    END
    $reserve$
    """


def _submit_file_function(quoted: str, digest_schema: str) -> str:
    presented = _token_hash(digest_schema)
    return f"""
    CREATE FUNCTION {quoted}.submit_public_evidence_with_file(
        p_token text,
        p_description text,
        p_reference text,
        p_submitter_name text,
        p_evidence_id uuid,
        p_storage_key text,
        p_filename text,
        p_media_type text,
        p_byte_size integer,
        p_checksum text
    )
    RETURNS uuid
    LANGUAGE plpgsql
    VOLATILE
    SECURITY DEFINER
    SET search_path = pg_catalog
    AS $submit_file$
    DECLARE
        v_link_id uuid;
        v_organization_id uuid;
        v_workflow_id uuid;
        v_requirement_id uuid;
        v_file_id uuid;
        v_description text;
        v_reference text;
        v_name text;
        v_authority text;
        v_updated integer;
    BEGIN
        SELECT
            links.id,
            links.organization_id,
            links.workflow_id,
            links.requirement_id
          INTO
            v_link_id,
            v_organization_id,
            v_workflow_id,
            v_requirement_id
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
           AND workflows.status IN ('draft', 'active')
         FOR UPDATE OF links;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
        PERFORM 1
          FROM {quoted}.workflows AS workflows
         WHERE workflows.id = v_workflow_id
           AND workflows.organization_id = v_organization_id
           AND workflows.status IN ('draft', 'active')
         FOR UPDATE;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
        PERFORM 1
          FROM {quoted}.requirements AS requirements
         WHERE requirements.id = v_requirement_id
           AND requirements.workflow_id = v_workflow_id
           AND requirements.status <> 'cancelled'
         FOR UPDATE;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;

        v_description := pg_catalog.btrim(p_description);
        v_reference := NULLIF(pg_catalog.btrim(COALESCE(p_reference, '')), '');
        v_name := NULLIF(pg_catalog.btrim(COALESCE(p_submitter_name, '')), '');
        IF v_reference IS NOT NULL THEN
            v_authority := pg_catalog.split_part(
                pg_catalog.substr(v_reference, 9), '/', 1
            );
            IF pg_catalog.length(v_reference) > 1000
               OR pg_catalog.strpos(v_reference, 'https://') <> 1
               OR v_reference ~ '[[:space:]]'
               OR v_reference ~ '[[:cntrl:]]'
               OR v_authority = ''
               OR pg_catalog.strpos(v_authority, '@') > 0 THEN
                RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
            END IF;
        END IF;
        IF v_description IS NULL
           OR pg_catalog.length(v_description) < 1
           OR pg_catalog.length(v_description) > 4000
           OR p_evidence_id IS NULL
           OR p_storage_key IS NULL
           OR p_filename IS NULL
           OR pg_catalog.length(pg_catalog.btrim(p_filename)) < 1
           OR pg_catalog.length(p_filename) > 200
           OR pg_catalog.strpos(p_filename, '/') > 0
           OR pg_catalog.strpos(p_filename, chr(92)) > 0
           OR p_filename ~ '[[:cntrl:]]'
           OR p_media_type NOT IN (
                'application/pdf',
                'image/png',
                'image/jpeg',
                'text/csv',
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
           )
           OR p_byte_size IS NULL
           OR p_byte_size < 1
           OR p_byte_size > 10485760
           OR p_checksum IS NULL
           OR p_checksum !~ '^[0-9a-f]{{64}}$'
           OR p_storage_key !~ '{_KEY_PATTERN}'
           OR pg_catalog.split_part(p_storage_key, '/', 1) <> v_organization_id::text
           OR pg_catalog.split_part(p_storage_key, '/', 2) <> p_evidence_id::text
           OR (v_name IS NOT NULL AND pg_catalog.length(v_name) > 200) THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;

        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended(p_storage_key, 0::bigint)
        );
        IF EXISTS (
            SELECT 1
              FROM {quoted}.{FILES} AS files
             WHERE files.storage_key = p_storage_key
        ) OR EXISTS (
            SELECT 1
              FROM {quoted}.{CLEANUP} AS cleanup
             WHERE cleanup.storage_key = p_storage_key
        ) THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;
        PERFORM 1
          FROM {quoted}.{RESERVATIONS} AS reservations
         WHERE reservations.storage_key = p_storage_key
           AND reservations.evidence_id = p_evidence_id
           AND reservations.organization_id = v_organization_id
           AND reservations.expires_at > pg_catalog.clock_timestamp()
         FOR UPDATE;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;

        UPDATE {quoted}.submission_links
           SET use_count = use_count + 1,
               last_used_at = pg_catalog.clock_timestamp()
         WHERE id = v_link_id
           AND revoked_at IS NULL
           AND expires_at > pg_catalog.clock_timestamp()
           AND use_count < max_uses;
        GET DIAGNOSTICS v_updated = ROW_COUNT;
        IF v_updated <> 1 THEN
            RETURN NULL;
        END IF;

        INSERT INTO {quoted}.evidence (
            id, requirement_id, description, evidence_type, submitted_by_id,
            submitted_at, reference, status, submitter_name, identity_verified
        )
        VALUES (
            p_evidence_id, v_requirement_id, v_description, 'document', NULL,
            pg_catalog.clock_timestamp(), v_reference, 'submitted', v_name, false
        );
        v_file_id := pg_catalog.gen_random_uuid();
        INSERT INTO {quoted}.{FILES} (
            id, evidence_id, organization_id, storage_key, original_filename,
            media_type, byte_size, checksum_sha256, uploaded_at, scan_status
        )
        VALUES (
            v_file_id, p_evidence_id, v_organization_id, p_storage_key,
            pg_catalog.btrim(p_filename), p_media_type, p_byte_size, p_checksum,
            pg_catalog.clock_timestamp(), 'unscanned'
        );
        DELETE FROM {quoted}.{RESERVATIONS}
         WHERE storage_key = p_storage_key;
        INSERT INTO {quoted}.audit_events (
            id, occurred_at, entity_type, entity_id, event_type,
            before_state, after_state, actor_reference, correlation_id
        )
        VALUES (
            pg_catalog.gen_random_uuid(),
            pg_catalog.clock_timestamp(),
            'workflow',
            v_workflow_id,
            'evidence.added',
            NULL,
            pg_catalog.json_build_object(
                'workflow_id', v_workflow_id::text,
                'organization_id', v_organization_id::text,
                'requirement_id', v_requirement_id::text,
                'evidence_id', p_evidence_id::text,
                'evidence_file_id', v_file_id::text,
                'status', 'submitted',
                'byte_size', p_byte_size,
                'media_type', p_media_type,
                'checksum_sha256', p_checksum
            ),
            'submission-link:' || v_link_id::text,
            v_link_id::text
        );
        RETURN p_evidence_id;
    END
    $submit_file$
    """


def _record_function(quoted: str) -> str:
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
            SELECT 1 FROM {quoted}.{FILES} AS files
             WHERE files.storage_key = p_storage_key
        ) THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;
        v_organization_id := pg_catalog.split_part(p_storage_key, '/', 1)::uuid;
        INSERT INTO {quoted}.{CLEANUP} (storage_key, organization_id, recorded_at)
        VALUES (p_storage_key, v_organization_id, pg_catalog.clock_timestamp())
        ON CONFLICT (storage_key) DO NOTHING;
    END
    $record$
    """


def _release_function(quoted: str) -> str:
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
            SELECT 1 FROM {quoted}.{FILES} AS files
             WHERE files.storage_key = p_storage_key
        ) THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;
        DELETE FROM {quoted}.{RESERVATIONS}
         WHERE storage_key = p_storage_key;
    END
    $release$
    """


def _claim_function(quoted: str) -> str:
    return f"""
    CREATE FUNCTION {quoted}.claim_evidence_storage_key(p_grace_seconds integer)
    RETURNS TABLE(claimed_storage_key text, claimed_token uuid)
    LANGUAGE plpgsql
    VOLATILE
    SECURITY DEFINER
    SET search_path = pg_catalog
    AS $claim$
    DECLARE
        v_key text;
        v_token uuid;
        v_old_token uuid;
        v_organization_id uuid;
        v_recorded_at timestamptz;
        v_cutoff timestamptz;
        v_attempt integer;
    BEGIN
        IF p_grace_seconds IS NULL OR p_grace_seconds < 86400 THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;
        v_cutoff := pg_catalog.clock_timestamp()
            - pg_catalog.make_interval(secs => p_grace_seconds);
        IF EXISTS (
            SELECT 1
              FROM {quoted}.{CLEANUP} AS cleanup
             WHERE cleanup.recorded_at <= v_cutoff
               AND cleanup.storage_key !~ '{_KEY_PATTERN}'
        ) OR EXISTS (
            SELECT 1
              FROM {quoted}.{RESERVATIONS} AS reservations
             WHERE reservations.expires_at <= v_cutoff
               AND reservations.storage_key !~ '{_KEY_PATTERN}'
        ) THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;
        FOR v_attempt IN 1..8 LOOP
            v_key := NULL;
            SELECT cleanup.storage_key
              INTO v_key
              FROM {quoted}.{CLEANUP} AS cleanup
             WHERE cleanup.claimed_at IS NULL
               AND cleanup.recorded_at <= v_cutoff
               AND cleanup.storage_key ~ '{_KEY_PATTERN}'
               AND NOT EXISTS (
                    SELECT 1 FROM {quoted}.{FILES} AS files
                     WHERE files.storage_key = cleanup.storage_key
               )
               AND NOT EXISTS (
                    SELECT 1 FROM {quoted}.{RESERVATIONS} AS reservations
                     WHERE reservations.storage_key = cleanup.storage_key
                       AND reservations.expires_at > pg_catalog.clock_timestamp()
               )
             ORDER BY cleanup.recorded_at
             LIMIT 1;
            IF v_key IS NOT NULL THEN
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(v_key, 0::bigint)
                );
                UPDATE {quoted}.{CLEANUP} AS cleanup
                   SET claimed_at = pg_catalog.clock_timestamp(),
                       claim_token = pg_catalog.gen_random_uuid()
                 WHERE cleanup.storage_key = v_key
                   AND cleanup.claimed_at IS NULL
                   AND cleanup.recorded_at <= v_cutoff
                   AND NOT EXISTS (
                        SELECT 1 FROM {quoted}.{FILES} AS files
                         WHERE files.storage_key = cleanup.storage_key
                   )
                   AND NOT EXISTS (
                        SELECT 1 FROM {quoted}.{RESERVATIONS} AS reservations
                         WHERE reservations.storage_key = cleanup.storage_key
                           AND reservations.expires_at > pg_catalog.clock_timestamp()
                   )
                RETURNING cleanup.claim_token INTO v_token;
                IF FOUND THEN
                    RETURN QUERY SELECT v_key, v_token;
                    RETURN;
                END IF;
                CONTINUE;
            END IF;

            SELECT reservations.storage_key
              INTO v_key
              FROM {quoted}.{RESERVATIONS} AS reservations
             WHERE reservations.expires_at <= v_cutoff
               AND reservations.storage_key ~ '{_KEY_PATTERN}'
               AND NOT EXISTS (
                    SELECT 1 FROM {quoted}.{FILES} AS files
                     WHERE files.storage_key = reservations.storage_key
               )
               AND NOT EXISTS (
                    SELECT 1 FROM {quoted}.{CLEANUP} AS cleanup
                     WHERE cleanup.storage_key = reservations.storage_key
               )
             ORDER BY reservations.expires_at
             LIMIT 1;
            IF v_key IS NOT NULL THEN
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(v_key, 0::bigint)
                );
                SELECT reservations.organization_id, reservations.reserved_at
                  INTO v_organization_id, v_recorded_at
                  FROM {quoted}.{RESERVATIONS} AS reservations
                 WHERE reservations.storage_key = v_key
                   AND reservations.expires_at <= v_cutoff
                 FOR UPDATE;
                IF FOUND
                   AND NOT EXISTS (
                        SELECT 1 FROM {quoted}.{FILES} AS files
                         WHERE files.storage_key = v_key
                   )
                   AND NOT EXISTS (
                        SELECT 1 FROM {quoted}.{CLEANUP} AS cleanup
                         WHERE cleanup.storage_key = v_key
                   ) THEN
                    v_token := pg_catalog.gen_random_uuid();
                    INSERT INTO {quoted}.{CLEANUP} (
                        storage_key, organization_id, recorded_at,
                        claimed_at, claim_token
                    )
                    VALUES (
                        v_key, v_organization_id, v_recorded_at,
                        pg_catalog.clock_timestamp(), v_token
                    );
                    DELETE FROM {quoted}.{RESERVATIONS}
                     WHERE storage_key = v_key;
                    RETURN QUERY SELECT v_key, v_token;
                    RETURN;
                END IF;
                CONTINUE;
            END IF;

            SELECT cleanup.storage_key, cleanup.claim_token
              INTO v_key, v_old_token
              FROM {quoted}.{CLEANUP} AS cleanup
             WHERE cleanup.claimed_at IS NOT NULL
               AND cleanup.claimed_at <=
                   pg_catalog.clock_timestamp() - interval '5 minutes'
               AND cleanup.recorded_at <= v_cutoff
               AND cleanup.storage_key ~ '{_KEY_PATTERN}'
               AND NOT EXISTS (
                    SELECT 1 FROM {quoted}.{FILES} AS files
                     WHERE files.storage_key = cleanup.storage_key
               )
               AND NOT EXISTS (
                    SELECT 1 FROM {quoted}.{RESERVATIONS} AS reservations
                     WHERE reservations.storage_key = cleanup.storage_key
                       AND reservations.expires_at > pg_catalog.clock_timestamp()
               )
             ORDER BY cleanup.claimed_at
             LIMIT 1;
            IF v_key IS NULL THEN
                RETURN;
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended(v_key, 0::bigint)
            );
            UPDATE {quoted}.{CLEANUP} AS cleanup
               SET claimed_at = pg_catalog.clock_timestamp(),
                   claim_token = pg_catalog.gen_random_uuid()
             WHERE cleanup.storage_key = v_key
               AND cleanup.claim_token = v_old_token
               AND NOT EXISTS (
                    SELECT 1 FROM {quoted}.{FILES} AS files
                     WHERE files.storage_key = cleanup.storage_key
               )
               AND NOT EXISTS (
                    SELECT 1 FROM {quoted}.{RESERVATIONS} AS reservations
                     WHERE reservations.storage_key = cleanup.storage_key
                       AND reservations.expires_at > pg_catalog.clock_timestamp()
               )
            RETURNING cleanup.claim_token INTO v_token;
            IF FOUND THEN
                RETURN QUERY SELECT v_key, v_token;
                RETURN;
            END IF;
        END LOOP;
        RETURN;
    END
    $claim$
    """


def _complete_cleanup_function(quoted: str) -> str:
    return f"""
    CREATE FUNCTION {quoted}.complete_evidence_storage_cleanup(
        p_storage_key text,
        p_claim_token uuid
    )
    RETURNS void
    LANGUAGE plpgsql
    VOLATILE
    SECURITY DEFINER
    SET search_path = pg_catalog
    AS $complete$
    BEGIN
        IF p_storage_key IS NULL
           OR p_claim_token IS NULL
           OR p_storage_key !~ '{_KEY_PATTERN}' THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended(p_storage_key, 0::bigint)
        );
        IF EXISTS (
            SELECT 1
              FROM {quoted}.{RESERVATIONS} AS reservations
             WHERE reservations.storage_key = p_storage_key
               AND reservations.expires_at > pg_catalog.clock_timestamp()
        ) THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;
        IF EXISTS (
            SELECT 1
              FROM {quoted}.{FILES} AS files
             WHERE files.storage_key = p_storage_key
        ) THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;
        DELETE FROM {quoted}.{CLEANUP} AS cleanup
         WHERE cleanup.storage_key = p_storage_key
           AND cleanup.claim_token = p_claim_token;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'invalid submission' USING ERRCODE = '23514';
        END IF;
        DELETE FROM {quoted}.{RESERVATIONS} AS reservations
         WHERE reservations.storage_key = p_storage_key
           AND reservations.expires_at <= pg_catalog.clock_timestamp();
    END
    $complete$
    """


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
        raise RuntimeError("refusing to enable evidence-file security in this schema")
    return _validated_identifier(schema)


def _validated_identifier(identifier: str) -> str:
    if not identifier or not identifier.replace("_", "").isalnum():
        raise RuntimeError("refusing an unexpected schema name")
    if identifier[0].isdigit():
        raise RuntimeError("refusing an unexpected schema name")
    return identifier


def _quote(identifier: str) -> str:
    return '"' + _validated_identifier(identifier).replace('"', "") + '"'
