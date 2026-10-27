"""Add revocable submission links and unverified public submitters.

Revision ID: 20261022_07
Revises: 20261018_06
Create Date: 2026-10-22

Existing evidence rows keep their person and are marked verified. Public
submissions may omit the person. PostgreSQL keeps the current tenant
policies. A no-login BYPASSRLS role owns two functions that read one link
and append one evidence row. They hash the presented token inside the
function, so the stored hash is not a usable credential. The application
role can read every link column except that hash. A trigger rejects any
later change to revoked_at. They reject a cancelled requirement and any
workflow that is not draft or active, and they lock those parent rows
before appending. PostgreSQL treats that row lock as an update privilege,
so the submission role receives UPDATE on status alone. The application
role does not receive that grant. When pgcrypto is absent, it is installed
in commitmentops_crypto and only the submission role may call digest. An
extension that is already installed is left in place. The application role
can execute the submission functions and cannot assume the owner role.
SQLite creates the tables and skips the functions.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from alembic import op
import sqlalchemy as sa

revision: str = "20261022_07"
down_revision: str | None = "20261018_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "commitmentops_app"
SUBMISSION_ROLE = "commitmentops_submission"
CRYPTO_SCHEMA = "commitmentops_crypto"
LINKS = "submission_links"
APP_LINK_COLUMNS = (
    "id",
    "organization_id",
    "workflow_id",
    "requirement_id",
    "created_at",
    "expires_at",
    "revoked_at",
    "created_by_user_id",
    "max_uses",
    "use_count",
    "last_used_at",
)
_SQLITE_REVOCATION_TRIGGER = """
CREATE TRIGGER submission_links_keep_revocation
BEFORE UPDATE ON submission_links
FOR EACH ROW
WHEN OLD.revoked_at IS NOT NULL
 AND NEW.revoked_at IS NOT OLD.revoked_at
BEGIN
    SELECT RAISE(ABORT, 'submission link revocation is permanent');
END
"""
LEGACY_ORGANIZATION_ID = UUID("00000000-0000-0000-0000-000000000001")
TENANT_POPULATED = (
    "Refusing to downgrade while non-legacy organizations, "
    "organization memberships, or non-legacy workflow assignments "
    "exist. Export or deliberately remove those tenant records "
    "before downgrading."
)
POPULATED = (
    "Refusing to downgrade while submission links exist. "
    "Export those records before downgrading."
)
UNVERIFIED_EVIDENCE = (
    "Refusing to downgrade while public evidence has no person submitter. "
    "Export those records before downgrading."
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
        CRYPTO_SCHEMA,
    }
)
_ROLE_STATEMENT = """
DO $role$
DECLARE
    existing record;
    migration_is_superuser boolean;
BEGIN
    SELECT rolsuper
      INTO migration_is_superuser
      FROM pg_roles
     WHERE rolname = current_user;
    IF NOT migration_is_superuser THEN
        RAISE EXCEPTION
            'Refusing to install public submission functions because the migration login is not a superuser. Forced row-level security would block a definer function owned by a normal role, and creating a BYPASSRLS owner requires a superuser. Existing policies were not changed.';
    END IF;
    SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
      INTO existing
      FROM pg_roles
     WHERE rolname = 'commitmentops_submission';
    IF NOT FOUND THEN
        CREATE ROLE commitmentops_submission
            NOLOGIN
            NOSUPERUSER
            BYPASSRLS
            NOCREATEDB
            NOCREATEROLE;
        SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
          INTO existing
          FROM pg_roles
         WHERE rolname = 'commitmentops_submission';
    END IF;
    IF existing.rolcanlogin
       OR existing.rolsuper
       OR NOT existing.rolbypassrls
       OR existing.rolcreatedb
       OR existing.rolcreaterole THEN
        RAISE EXCEPTION
            'Refusing to use existing role commitmentops_submission because it is not NOLOGIN, NOSUPERUSER, BYPASSRLS, NOCREATEDB, and NOCREATEROLE';
    END IF;
END
$role$
"""


def upgrade() -> None:
    _widen_evidence()
    _create_links()
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    schema = _current_schema(bind)
    quoted_schema = _quote(schema)
    _install_postgres_boundary(bind, schema, quoted_schema)


def downgrade() -> None:
    bind = op.get_bind()
    if not op.get_context().as_sql:
        _refuse_populated_tenant_downgrade(bind)
        populated = bind.execute(sa.text(f"SELECT COUNT(*) FROM {LINKS}")).scalar()
        if populated:
            raise RuntimeError(POPULATED)
        unverified = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM evidence WHERE submitted_by_id IS NULL"
            )
        ).scalar()
        if unverified:
            raise RuntimeError(UNVERIFIED_EVIDENCE)
    if bind.dialect.name == "postgresql":
        schema = _current_schema(bind)
        quoted_schema = _quote(schema)
        _drop_postgres_boundary(bind, quoted_schema)
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS submission_links_keep_revocation")
    op.drop_table(LINKS)
    _restore_evidence()


def _widen_evidence() -> None:
    columns = (
        sa.Column("submitter_name", sa.String(length=200), nullable=True),
        sa.Column(
            "identity_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    check = (
        "(identity_verified = true AND submitted_by_id IS NOT NULL) "
        "OR (identity_verified = false AND submitted_by_id IS NULL)"
    )
    if op.get_bind().dialect.name == "postgresql":
        for column in columns:
            op.add_column("evidence", column)
        op.alter_column("evidence", "submitted_by_id", nullable=True)
        op.create_check_constraint(
            "ck_evidence_submitter_identity",
            "evidence",
            check,
        )
        return
    with op.batch_alter_table("evidence") as batch_op:
        for column in columns:
            batch_op.add_column(column)
        batch_op.alter_column(
            "submitted_by_id",
            existing_type=sa.Uuid(),
            nullable=True,
        )
        batch_op.create_check_constraint(
            "ck_evidence_submitter_identity",
            check,
        )


def _restore_evidence() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(
            "ck_evidence_submitter_identity",
            "evidence",
            type_="check",
        )
        op.drop_column("evidence", "submitter_name")
        op.drop_column("evidence", "identity_verified")
        op.alter_column("evidence", "submitted_by_id", nullable=False)
        return
    with op.batch_alter_table("evidence") as batch_op:
        batch_op.drop_constraint(
            "ck_evidence_submitter_identity",
            type_="check",
        )
        batch_op.drop_column("submitter_name")
        batch_op.drop_column("identity_verified")
        batch_op.alter_column(
            "submitted_by_id",
            existing_type=sa.Uuid(),
            nullable=False,
        )


def _create_links() -> None:
    op.create_table(
        LINKS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("requirement_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column(
            "use_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(token_hash) = 64",
            name="ck_submission_links_token_hash",
        ),
        sa.CheckConstraint(
            "use_count >= 0",
            name="ck_submission_links_use_count",
        ),
        sa.CheckConstraint(
            "max_uses >= 1",
            name="ck_submission_links_max_uses",
        ),
        sa.CheckConstraint(
            "use_count <= max_uses",
            name="ck_submission_links_use_limit",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_submission_links_organization_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id", "organization_id"],
            ["workflows.id", "workflows.organization_id"],
            name="fk_submission_links_workflow_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requirement_id", "workflow_id"],
            ["requirements.id", "requirements.workflow_id"],
            name="fk_submission_links_requirement_workflow",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "token_hash",
            name="uq_submission_links_token_hash",
        ),
    )
    op.create_index(
        "ix_submission_links_requirement",
        LINKS,
        ["organization_id", "workflow_id", "requirement_id"],
    )
    if op.get_bind().dialect.name == "sqlite":
        op.execute(_SQLITE_REVOCATION_TRIGGER)


def _install_postgres_boundary(
    bind: sa.Connection,
    schema: str,
    quoted_schema: str,
) -> None:
    bind.execute(sa.text(_ROLE_STATEMENT))
    qualified_links = f"{quoted_schema}.{_quote(LINKS)}"
    bind.execute(
        sa.text(f"GRANT USAGE ON SCHEMA {quoted_schema} TO {SUBMISSION_ROLE}")
    )
    digest_schema = _prepare_digest(bind)
    bind.execute(
        sa.text(
            "GRANT SELECT, UPDATE (use_count, last_used_at) "
            f"ON {qualified_links} TO {SUBMISSION_ROLE}"
        )
    )
    for table in ("requirements", "workflows", "organizations"):
        bind.execute(
            sa.text(
                f"GRANT SELECT ON {quoted_schema}.{_quote(table)} "
                f"TO {SUBMISSION_ROLE}"
            )
        )
    for table in ("requirements", "workflows"):
        bind.execute(
            sa.text(
                f"GRANT UPDATE (status) ON {quoted_schema}.{_quote(table)} "
                f"TO {SUBMISSION_ROLE}"
            )
        )
    bind.execute(
        sa.text(
            f"GRANT INSERT ON {quoted_schema}.{_quote('evidence')} "
            f"TO {SUBMISSION_ROLE}"
        )
    )
    bind.execute(
        sa.text(
            f"GRANT INSERT ON {quoted_schema}.{_quote('audit_events')} "
            f"TO {SUBMISSION_ROLE}"
        )
    )
    readable = ", ".join(_quote(column) for column in APP_LINK_COLUMNS)
    bind.execute(
        sa.text(
            f"GRANT SELECT ({readable}) ON {qualified_links} TO {APP_ROLE}"
        )
    )
    bind.execute(
        sa.text(f"GRANT INSERT ON {qualified_links} TO {APP_ROLE}")
    )
    bind.execute(
        sa.text(
            f"GRANT UPDATE (revoked_at) ON {qualified_links} TO {APP_ROLE}"
        )
    )
    bind.execute(
        sa.text(
            f"""
            CREATE POLICY submission_links_tenant ON {qualified_links}
            FOR ALL
            USING (
                organization_id = {quoted_schema}.app_current_organization_id()
                AND {quoted_schema}.app_is_active_member(organization_id)
            )
            WITH CHECK (
                organization_id = {quoted_schema}.app_current_organization_id()
                AND {quoted_schema}.app_is_active_member(organization_id)
            )
            """
        )
    )
    bind.execute(
        sa.text(
            f"ALTER TABLE {qualified_links} ENABLE ROW LEVEL SECURITY"
        )
    )
    bind.execute(
        sa.text(f"ALTER TABLE {qualified_links} FORCE ROW LEVEL SECURITY")
    )
    guard = f"{quoted_schema}.submission_links_keep_revocation()"
    bind.execute(sa.text(_revocation_guard(quoted_schema)))
    bind.execute(sa.text(f"REVOKE ALL ON FUNCTION {guard} FROM PUBLIC"))
    bind.execute(sa.text(f"GRANT EXECUTE ON FUNCTION {guard} TO {APP_ROLE}"))
    bind.execute(
        sa.text(f"GRANT EXECUTE ON FUNCTION {guard} TO {SUBMISSION_ROLE}")
    )
    bind.execute(
        sa.text(
            f"CREATE TRIGGER submission_links_keep_revocation "
            f"BEFORE UPDATE ON {qualified_links} "
            f"FOR EACH ROW EXECUTE PROCEDURE {guard}"
        )
    )
    for statement in (
        _context_function(quoted_schema, digest_schema),
        _submit_function(quoted_schema, digest_schema),
    ):
        bind.execute(sa.text(statement))
    for signature in (
        "public_submission_context(text)",
        "submit_public_evidence(text, text, text, text)",
    ):
        qualified = f"{quoted_schema}.{signature}"
        bind.execute(sa.text(f"REVOKE ALL ON FUNCTION {qualified} FROM PUBLIC"))
        bind.execute(
            sa.text(f"GRANT EXECUTE ON FUNCTION {qualified} TO {APP_ROLE}")
        )
        bind.execute(
            sa.text(f"ALTER FUNCTION {qualified} OWNER TO {SUBMISSION_ROLE}")
        )
        bind.execute(sa.text(f"REVOKE ALL ON FUNCTION {qualified} FROM PUBLIC"))
        bind.execute(
            sa.text(f"GRANT EXECUTE ON FUNCTION {qualified} TO {APP_ROLE}")
        )


def _drop_postgres_boundary(bind: sa.Connection, quoted_schema: str) -> None:
    qualified_links = f"{quoted_schema}.{_quote(LINKS)}"
    bind.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS submission_links_keep_revocation "
            f"ON {qualified_links}"
        )
    )
    bind.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS "
            f"{quoted_schema}.submission_links_keep_revocation()"
        )
    )
    bind.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS "
            f"{quoted_schema}.submit_public_evidence(text, text, text, text)"
        )
    )
    bind.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS "
            f"{quoted_schema}.public_submission_context(text)"
        )
    )
    bind.execute(
        sa.text(
            "DROP POLICY IF EXISTS submission_links_tenant "
            f"ON {qualified_links}"
        )
    )
    for table in ("requirements", "workflows"):
        bind.execute(
            sa.text(
                f"REVOKE UPDATE (status) ON {quoted_schema}.{_quote(table)} "
                f"FROM {SUBMISSION_ROLE}"
            )
        )


def _prepare_digest(bind: sa.Connection) -> str:
    """Return the schema-qualified home of pgcrypto's digest function.

    An existing extension stays in its current schema, with its grants
    unchanged. A missing extension is installed in a private schema whose
    only exposed function is digest(bytea, text).
    """

    if op.get_context().as_sql:
        for statement in _crypto_install_statements():
            bind.execute(sa.text(statement))
        return _quote(CRYPTO_SCHEMA)
    installed = _installed_extension_schema(bind)
    if installed is not None:
        return _quote(_validated_identifier(installed))
    _install_private_pgcrypto(bind)
    return _quote(CRYPTO_SCHEMA)


def _installed_extension_schema(bind: sa.Connection) -> str | None:
    installed = bind.execute(
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
    if installed is None:
        return None
    if not isinstance(installed, str):
        raise RuntimeError("pgcrypto is unavailable")
    return installed


def _install_private_pgcrypto(bind: sa.Connection) -> None:
    quoted = _quote(CRYPTO_SCHEMA)
    existing = bind.execute(
        sa.text("SELECT 1 FROM pg_namespace WHERE nspname = :name"),
        {"name": CRYPTO_SCHEMA},
    ).scalar()
    if existing is None:
        bind.execute(sa.text(f"CREATE SCHEMA {quoted}"))
    else:
        occupied = bind.execute(
            sa.text(
                """
                SELECT COUNT(*)
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = :name
                  AND relation.relkind IN ('r', 'v', 'm', 'f', 'p', 'S')
                """
            ),
            {"name": CRYPTO_SCHEMA},
        ).scalar()
        routines = bind.execute(
            sa.text(
                """
                SELECT COUNT(*)
                FROM pg_proc AS routine
                JOIN pg_namespace AS namespace
                  ON namespace.oid = routine.pronamespace
                WHERE namespace.nspname = :name
                """
            ),
            {"name": CRYPTO_SCHEMA},
        ).scalar()
        if occupied or routines:
            raise RuntimeError(
                "Refusing to install pgcrypto into an occupied schema"
            )
    for statement in _crypto_install_statements(include_create_schema=False):
        bind.execute(sa.text(statement))


def _crypto_install_statements(
    *,
    include_create_schema: bool = True,
) -> tuple[str, ...]:
    quoted = _quote(CRYPTO_SCHEMA)
    statements = []
    if include_create_schema:
        statements.append(f"CREATE SCHEMA {quoted}")
    statements.extend(
        (
            f"REVOKE ALL ON SCHEMA {quoted} FROM PUBLIC",
            f"GRANT USAGE ON SCHEMA {quoted} TO {SUBMISSION_ROLE}",
            f"CREATE EXTENSION pgcrypto SCHEMA {quoted}",
            f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {quoted} FROM PUBLIC",
            (
                f"GRANT EXECUTE ON FUNCTION {quoted}.digest(bytea, text) "
                f"TO {SUBMISSION_ROLE}"
            ),
        )
    )
    return tuple(statements)


def _token_digest_sql(digest_schema: str) -> str:
    """Hash the presented token inside the definer function."""

    return (
        "CASE WHEN pg_catalog.length(p_token) BETWEEN 1 AND 200 THEN "
        "pg_catalog.encode("
        f"{digest_schema}.digest("
        "pg_catalog.convert_to(p_token, 'UTF8'), 'sha256'), 'hex') "
        "ELSE NULL END"
    )


def _revocation_guard(quoted_schema: str) -> str:
    return f"""
    CREATE FUNCTION {quoted_schema}.submission_links_keep_revocation()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog
    AS $guard$
    BEGIN
        IF OLD.revoked_at IS NOT NULL
           AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
            RAISE EXCEPTION 'submission link revocation is permanent'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END
    $guard$
    """


def _context_function(quoted_schema: str, digest_schema: str) -> str:
    presented_hash = _token_digest_sql(digest_schema)
    return f"""
    CREATE FUNCTION {quoted_schema}.public_submission_context(p_token text)
    RETURNS TABLE (
        organization_name text,
        requirement_title text,
        evidence_expectation text
    )
    LANGUAGE plpgsql
    VOLATILE
    SECURITY DEFINER
    SET search_path = pg_catalog
    AS $context$
    BEGIN
        RETURN QUERY
        SELECT
            COALESCE(
                NULLIF(pg_catalog.btrim(organizations.display_name), ''),
                organizations.name
            )::text,
            requirements.title::text,
            requirements.evidence_expectation::text
        FROM {quoted_schema}.submission_links AS links
        JOIN {quoted_schema}.requirements AS requirements
          ON requirements.id = links.requirement_id
         AND requirements.workflow_id = links.workflow_id
        JOIN {quoted_schema}.workflows AS workflows
          ON workflows.id = links.workflow_id
         AND workflows.organization_id = links.organization_id
        JOIN {quoted_schema}.organizations AS organizations
          ON organizations.id = links.organization_id
        WHERE links.token_hash = {presented_hash}
          AND links.revoked_at IS NULL
          AND links.expires_at > pg_catalog.clock_timestamp()
          AND links.use_count < links.max_uses
          AND requirements.status <> 'cancelled'
          AND workflows.status IN ('draft', 'active');
    END
    $context$
    """


def _submit_function(quoted_schema: str, digest_schema: str) -> str:
    presented_hash = _token_digest_sql(digest_schema)
    return f"""
    CREATE FUNCTION {quoted_schema}.submit_public_evidence(
        p_token text,
        p_description text,
        p_reference text,
        p_submitter_name text
    )
    RETURNS uuid
    LANGUAGE plpgsql
    VOLATILE
    SECURITY DEFINER
    SET search_path = pg_catalog
    AS $submit$
    DECLARE
        v_link_id uuid;
        v_organization_id uuid;
        v_workflow_id uuid;
        v_requirement_id uuid;
        v_evidence_id uuid;
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
          FROM {quoted_schema}.submission_links AS links
          JOIN {quoted_schema}.requirements AS requirements
            ON requirements.id = links.requirement_id
           AND requirements.workflow_id = links.workflow_id
          JOIN {quoted_schema}.workflows AS workflows
            ON workflows.id = links.workflow_id
           AND workflows.organization_id = links.organization_id
         WHERE links.token_hash = {presented_hash}
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
          FROM {quoted_schema}.workflows AS workflows
         WHERE workflows.id = v_workflow_id
           AND workflows.organization_id = v_organization_id
           AND workflows.status IN ('draft', 'active')
         FOR UPDATE;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
        PERFORM 1
          FROM {quoted_schema}.requirements AS requirements
         WHERE requirements.id = v_requirement_id
           AND requirements.workflow_id = v_workflow_id
           AND requirements.status <> 'cancelled'
         FOR UPDATE;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;

        v_description := pg_catalog.btrim(p_description);
        v_reference := pg_catalog.btrim(p_reference);
        v_name := NULLIF(
            pg_catalog.btrim(COALESCE(p_submitter_name, '')),
            ''
        );
        v_authority := pg_catalog.split_part(
            pg_catalog.substr(COALESCE(v_reference, ''), 9),
            '/',
            1
        );
        IF v_description IS NULL
           OR pg_catalog.length(v_description) < 1
           OR pg_catalog.length(v_description) > 4000
           OR v_reference IS NULL
           OR pg_catalog.length(v_reference) > 1000
           OR pg_catalog.strpos(v_reference, 'https://') <> 1
           OR v_reference ~ '[[:space:]]'
           OR v_reference ~ '[[:cntrl:]]'
           OR v_authority = ''
           OR pg_catalog.strpos(v_authority, '@') > 0 THEN
            RAISE EXCEPTION 'invalid submission'
                USING ERRCODE = '23514';
        END IF;
        IF v_name IS NOT NULL AND pg_catalog.length(v_name) > 200 THEN
            RAISE EXCEPTION 'invalid submission'
                USING ERRCODE = '23514';
        END IF;

        UPDATE {quoted_schema}.submission_links
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

        v_evidence_id := pg_catalog.gen_random_uuid();
        INSERT INTO {quoted_schema}.evidence (
            id,
            requirement_id,
            description,
            evidence_type,
            submitted_by_id,
            submitted_at,
            reference,
            status,
            submitter_name,
            identity_verified
        )
        VALUES (
            v_evidence_id,
            v_requirement_id,
            v_description,
            'link',
            NULL,
            pg_catalog.clock_timestamp(),
            v_reference,
            'submitted',
            v_name,
            false
        );
        INSERT INTO {quoted_schema}.audit_events (
            id,
            occurred_at,
            entity_type,
            entity_id,
            event_type,
            before_state,
            after_state,
            actor_reference,
            correlation_id
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
                'evidence_id', v_evidence_id::text,
                'status', 'submitted'
            ),
            'submission-link:' || v_link_id::text,
            v_link_id::text
        );
        RETURN v_evidence_id;
    END
    $submit$
    """


def _refuse_populated_tenant_downgrade(bind: sa.Connection) -> None:
    organizations = sa.table("organizations", sa.column("id", sa.Uuid()))
    workflows = sa.table(
        "workflows",
        sa.column("organization_id", sa.Uuid()),
    )
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


def _current_schema(bind: sa.Connection) -> str:
    if op.get_context().as_sql:
        return "public"
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    if not isinstance(schema, str) or schema.lower() in _DENIED_SCHEMAS:
        raise RuntimeError(
            "refusing to enable submission-link security in this schema"
        )
    return _validated_identifier(schema)


def _validated_identifier(identifier: str) -> str:
    if not identifier or not identifier.replace("_", "").isalnum():
        raise RuntimeError("refusing an unexpected schema name")
    if identifier[0].isdigit():
        raise RuntimeError("refusing an unexpected schema name")
    return identifier


def _quote(identifier: str) -> str:
    return '"' + _validated_identifier(identifier).replace('"', "") + '"'
