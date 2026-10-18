"""Enforce tenant isolation with PostgreSQL row-level security.

Revision ID: 20261017_04
Revises: 20261016_03
Create Date: 2026-10-17

SQLite upgrades record this revision without policies. PostgreSQL creates a
non-login application role, or refuses an existing role whose attributes are
not already safe. It forces row-level security and grants that role only the
DML the application uses. Membership is granted to the migration login and,
when a different runtime login was supplied, to that login after its
attributes and ownership are checked. The role is cluster-wide and is not
dropped on downgrade. This migration does not alter an existing role and does
not grant superuser, bypass, ownership, or schema privileges to the runtime
login. Person inserts are allowed only when the same transaction already
references that person from a requirement or evidence row the caller can
write. The foreign keys that point at people are checked at commit so those
references can be written first. People are not assigned to an organization.
PostgreSQL downgrade is refused so those foreign keys stay deferred.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from alembic import op
import sqlalchemy as sa

from app.persistence.database import RUNTIME_LOGIN_ATTRIBUTE
from app.persistence.rls import runtime_login_block_reason

LEGACY_ORGANIZATION_ID = UUID("00000000-0000-0000-0000-000000000001")
APP_ROLE = "commitmentops_app"
TENANT_TABLES = (
    "organizations",
    "organization_memberships",
    "people",
    "workflows",
    "requirements",
    "evidence",
    "audit_events",
)
READ_TABLES = ("organizations", "organization_memberships")
WRITE_TABLES = ("people", "workflows", "requirements", "evidence")
INSERT_TABLES = ("audit_events",)

revision: str = "20261017_04"
down_revision: str | None = "20261016_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REFUSAL = (
    "Refusing to downgrade while non-legacy organizations, "
    "organization memberships, or non-legacy workflow assignments "
    "exist. Export or deliberately remove those tenant records "
    "before downgrading."
)
DEFERRED_PERSON_FK_REFUSAL = (
    "Refusing to downgrade because person foreign keys must stay deferred. "
    "The application writes requirement and evidence references before person "
    "rows, and immediate checks would reject those saves. Keep revision "
    "20261017_04 applied."
)
UNSAFE_ROLE = (
    "Refusing to use existing role commitmentops_app because it is not "
    "NOLOGIN, NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, and NOCREATEROLE"
)
_ROLE_STATEMENT = """
DO $role$
DECLARE
    existing record;
BEGIN
    SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
      INTO existing
      FROM pg_roles
     WHERE rolname = 'commitmentops_app';
    IF NOT FOUND THEN
        BEGIN
            CREATE ROLE commitmentops_app
                NOLOGIN
                NOSUPERUSER
                NOBYPASSRLS
                NOCREATEDB
                NOCREATEROLE;
        EXCEPTION
            WHEN duplicate_object THEN
                NULL;
        END;
        SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
          INTO existing
          FROM pg_roles
         WHERE rolname = 'commitmentops_app';
    END IF;
    IF existing.rolcanlogin
       OR existing.rolsuper
       OR existing.rolbypassrls
       OR existing.rolcreatedb
       OR existing.rolcreaterole THEN
        RAISE EXCEPTION
            'Refusing to use existing role commitmentops_app because it is not NOLOGIN, NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, and NOCREATEROLE';
    END IF;
END
$role$
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    schema = _current_schema(bind)
    quoted_schema = _quote(schema)
    _create_application_role(bind)
    bind.execute(
        sa.text(f"GRANT USAGE ON SCHEMA {quoted_schema} TO {APP_ROLE}")
    )
    for table in READ_TABLES:
        bind.execute(
            sa.text(
                f"GRANT SELECT ON {quoted_schema}.{_quote(table)} TO {APP_ROLE}"
            )
        )
    for table in WRITE_TABLES:
        bind.execute(
            sa.text(
                "GRANT SELECT, INSERT, UPDATE ON "
                f"{quoted_schema}.{_quote(table)} TO {APP_ROLE}"
            )
        )
    for table in INSERT_TABLES:
        bind.execute(
            sa.text(
                "GRANT SELECT, INSERT ON "
                f"{quoted_schema}.{_quote(table)} TO {APP_ROLE}"
            )
        )
    _create_functions(bind, quoted_schema)
    _create_policies(bind, quoted_schema)
    for table in TENANT_TABLES:
        bind.execute(
            sa.text(
                f"ALTER TABLE {quoted_schema}.{_quote(table)} "
                "ENABLE ROW LEVEL SECURITY"
            )
        )
        bind.execute(
            sa.text(
                f"ALTER TABLE {quoted_schema}.{_quote(table)} "
                "FORCE ROW LEVEL SECURITY"
            )
        )
    _set_person_foreign_keys(bind, quoted_schema, deferred=True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql("SET LOCAL row_security = off")
        _require_privileged_migration_connection(bind)
    _refuse_populated_tenant_downgrade(bind)
    if bind.dialect.name != "postgresql":
        return
    raise RuntimeError(DEFERRED_PERSON_FK_REFUSAL)


def _create_application_role(bind: sa.Connection) -> None:
    if op.get_context().as_sql:
        bind.execute(sa.text(_ROLE_STATEMENT))
        _grant_known_logins(bind, inspect_catalog=False)
        return
    _reject_unsafe_application_role(bind)
    bind.execute(sa.text(_ROLE_STATEMENT))
    _grant_known_logins(bind, inspect_catalog=True)


def _reject_unsafe_application_role(bind: sa.Connection) -> None:
    existing = bind.execute(
        sa.text(
            """
            SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
            FROM pg_roles
            WHERE rolname = :role
            """
        ),
        {"role": APP_ROLE},
    ).one_or_none()
    if existing is None:
        return
    if (
        existing.rolcanlogin
        or existing.rolsuper
        or existing.rolbypassrls
        or existing.rolcreatedb
        or existing.rolcreaterole
    ):
        raise RuntimeError(UNSAFE_ROLE)


def _grant_known_logins(bind: sa.Connection, *, inspect_catalog: bool) -> None:
    runtime = _runtime_login_from_config()
    if inspect_catalog and runtime is not None:
        reason = runtime_login_block_reason(bind, runtime)
        if reason is not None:
            raise RuntimeError(reason)
    bind.execute(sa.text(f"GRANT {APP_ROLE} TO CURRENT_USER"))
    if runtime is None:
        return
    if inspect_catalog:
        current_login = bind.execute(sa.text("SELECT current_user")).scalar()
        if runtime != current_login:
            bind.execute(sa.text(f"GRANT {APP_ROLE} TO {_quote(runtime)}"))
    else:
        bind.execute(sa.text(f"GRANT {APP_ROLE} TO {_quote(runtime)}"))
        return
    can_assume = bind.execute(
        sa.text("SELECT pg_has_role(:runtime, :app_role, 'MEMBER')"),
        {"runtime": runtime, "app_role": APP_ROLE},
    ).scalar()
    if not can_assume:
        raise RuntimeError(
            "Refusing to continue because the runtime login cannot assume "
            "commitmentops_app"
        )


def _runtime_login_from_config() -> str | None:
    value = op.get_context().config.attributes.get(RUNTIME_LOGIN_ATTRIBUTE)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise RuntimeError("refusing an unexpected runtime login")
    return _validated_identifier(value)


def _create_functions(bind: sa.Connection, quoted_schema: str) -> None:
    bind.execute(
        sa.text(
            f"""
            CREATE FUNCTION {quoted_schema}.app_current_user_id()
            RETURNS uuid
            LANGUAGE plpgsql
            STABLE
            SET search_path = pg_catalog
            AS $body$
            DECLARE
                raw text;
            BEGIN
                raw := current_setting('app.user_id', true);
                IF raw IS NULL OR btrim(raw) = '' THEN
                    RETURN NULL;
                END IF;
                RETURN raw::uuid;
            EXCEPTION
                WHEN invalid_text_representation THEN
                    RETURN NULL;
            END
            $body$
            """
        )
    )
    bind.execute(
        sa.text(
            f"""
            CREATE FUNCTION {quoted_schema}.app_current_organization_id()
            RETURNS uuid
            LANGUAGE plpgsql
            STABLE
            SET search_path = pg_catalog
            AS $body$
            DECLARE
                raw text;
            BEGIN
                raw := current_setting('app.organization_id', true);
                IF raw IS NULL OR btrim(raw) = '' THEN
                    RETURN NULL;
                END IF;
                RETURN raw::uuid;
            EXCEPTION
                WHEN invalid_text_representation THEN
                    RETURN NULL;
            END
            $body$
            """
        )
    )
    bind.execute(
        sa.text(
            f"""
            CREATE FUNCTION {quoted_schema}.app_is_active_member(
                target_organization uuid
            )
            RETURNS boolean
            LANGUAGE sql
            STABLE
            SET search_path = pg_catalog
            AS $body$
                SELECT EXISTS (
                    SELECT 1
                    FROM {quoted_schema}.organization_memberships
                    WHERE organization_id = target_organization
                      AND user_id = {quoted_schema}.app_current_user_id()
                      AND is_active
                )
            $body$
            """
        )
    )


def _create_policies(bind: sa.Connection, quoted_schema: str) -> None:
    user_fn = f"{quoted_schema}.app_current_user_id()"
    org_fn = f"{quoted_schema}.app_current_organization_id()"
    member_fn = f"{quoted_schema}.app_is_active_member"
    workflows = f"{quoted_schema}.workflows"
    requirements = f"{quoted_schema}.requirements"
    evidence = f"{quoted_schema}.evidence"
    memberships = f"{quoted_schema}.organization_memberships"
    visible_workflow = f"""
        EXISTS (
            SELECT 1
            FROM {workflows} AS tenant_workflow
            WHERE tenant_workflow.id = {{workflow_id}}
              AND tenant_workflow.organization_id = {org_fn}
              AND {member_fn}(tenant_workflow.organization_id)
        )
    """
    statements = [
        f"""
        CREATE POLICY organizations_tenant ON {quoted_schema}.organizations
        FOR SELECT
        USING (
            EXISTS (
                SELECT 1
                FROM {memberships} AS tenant_membership
                WHERE tenant_membership.organization_id = {quoted_schema}.organizations.id
                  AND tenant_membership.user_id = {user_fn}
                  AND tenant_membership.is_active
            )
        )
        """,
        f"""
        CREATE POLICY organization_memberships_tenant
        ON {quoted_schema}.organization_memberships
        FOR SELECT
        USING (user_id = {user_fn})
        """,
        f"""
        CREATE POLICY workflows_tenant ON {workflows}
        FOR ALL
        USING (
            organization_id = {org_fn}
            AND {member_fn}(organization_id)
        )
        WITH CHECK (
            organization_id = {org_fn}
            AND {member_fn}(organization_id)
        )
        """,
        f"""
        CREATE POLICY requirements_tenant ON {requirements}
        FOR ALL
        USING (
            {visible_workflow.format(workflow_id=f"{requirements}.workflow_id")}
        )
        WITH CHECK (
            {visible_workflow.format(workflow_id=f"{requirements}.workflow_id")}
        )
        """,
        f"""
        CREATE POLICY evidence_tenant ON {evidence}
        FOR ALL
        USING (
            EXISTS (
                SELECT 1
                FROM {requirements} AS tenant_requirement
                WHERE tenant_requirement.id = {evidence}.requirement_id
                  AND {visible_workflow.format(workflow_id="tenant_requirement.workflow_id")}
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1
                FROM {requirements} AS tenant_requirement
                WHERE tenant_requirement.id = {evidence}.requirement_id
                  AND {visible_workflow.format(workflow_id="tenant_requirement.workflow_id")}
            )
        )
        """,
        f"""
        CREATE POLICY audit_events_tenant ON {quoted_schema}.audit_events
        FOR ALL
        USING (
            entity_type = 'workflow'
            AND {visible_workflow.format(workflow_id=f"{quoted_schema}.audit_events.entity_id")}
        )
        WITH CHECK (
            entity_type = 'workflow'
            AND {visible_workflow.format(workflow_id=f"{quoted_schema}.audit_events.entity_id")}
        )
        """,
        f"""
        CREATE POLICY people_tenant ON {quoted_schema}.people
        FOR SELECT
        USING (
            id = {user_fn}
            OR EXISTS (
                SELECT 1
                FROM {requirements} AS tenant_requirement
                WHERE (
                    tenant_requirement.owner_id = {quoted_schema}.people.id
                    OR tenant_requirement.supervisor_id = {quoted_schema}.people.id
                    OR tenant_requirement.reviewed_by_id = {quoted_schema}.people.id
                )
                AND {visible_workflow.format(workflow_id="tenant_requirement.workflow_id")}
            )
            OR EXISTS (
                SELECT 1
                FROM {evidence} AS tenant_evidence
                JOIN {requirements} AS tenant_requirement
                  ON tenant_requirement.id = tenant_evidence.requirement_id
                WHERE tenant_evidence.submitted_by_id = {quoted_schema}.people.id
                  AND {visible_workflow.format(workflow_id="tenant_requirement.workflow_id")}
            )
        )
        """,
        f"""
        CREATE POLICY people_insert_tenant ON {quoted_schema}.people
        FOR INSERT
        WITH CHECK (
            EXISTS (
                SELECT 1
                FROM {requirements} AS tenant_requirement
                WHERE (
                    tenant_requirement.owner_id = {quoted_schema}.people.id
                    OR tenant_requirement.supervisor_id = {quoted_schema}.people.id
                    OR tenant_requirement.reviewed_by_id = {quoted_schema}.people.id
                )
                AND {visible_workflow.format(workflow_id="tenant_requirement.workflow_id")}
            )
            OR EXISTS (
                SELECT 1
                FROM {evidence} AS tenant_evidence
                JOIN {requirements} AS tenant_requirement
                  ON tenant_requirement.id = tenant_evidence.requirement_id
                WHERE tenant_evidence.submitted_by_id = {quoted_schema}.people.id
                  AND {visible_workflow.format(workflow_id="tenant_requirement.workflow_id")}
            )
        )
        """,
    ]
    for statement in statements:
        bind.execute(sa.text(statement))


_PERSON_REFERENCE_COLUMNS = {
    ("requirements", "owner_id"),
    ("requirements", "supervisor_id"),
    ("requirements", "reviewed_by_id"),
    ("evidence", "submitted_by_id"),
}
_PERSON_REFERENCE_CONSTRAINTS = (
    ("requirements", "requirements_owner_id_fkey"),
    ("requirements", "requirements_supervisor_id_fkey"),
    ("requirements", "requirements_reviewed_by_id_fkey"),
    ("evidence", "evidence_submitted_by_id_fkey"),
)


def _set_person_foreign_keys(
    bind: sa.Connection,
    quoted_schema: str,
    *,
    deferred: bool,
) -> None:
    timing = (
        "DEFERRABLE INITIALLY DEFERRED"
        if deferred
        else "NOT DEFERRABLE"
    )
    for table_name, constraint_name in _person_foreign_keys(bind):
        bind.execute(
            sa.text(
                f"ALTER TABLE {quoted_schema}.{_quote(table_name)} "
                f"ALTER CONSTRAINT {_quote(constraint_name)} {timing}"
            )
        )


def _person_foreign_keys(bind: sa.Connection) -> tuple[tuple[str, str], ...]:
    if op.get_context().as_sql:
        return _PERSON_REFERENCE_CONSTRAINTS
    rows = bind.execute(
        sa.text(
            """
            SELECT rel.relname, pg_constraint.conname, attribute.attname
            FROM pg_constraint
            JOIN pg_class AS rel ON rel.oid = pg_constraint.conrelid
            JOIN pg_namespace ON pg_namespace.oid = rel.relnamespace
            JOIN pg_class AS referenced
              ON referenced.oid = pg_constraint.confrelid
            JOIN pg_attribute AS attribute
              ON attribute.attrelid = rel.oid
             AND attribute.attnum = pg_constraint.conkey[1]
            WHERE pg_namespace.nspname = current_schema()
              AND pg_constraint.contype = 'f'
              AND referenced.relname = 'people'
              AND array_length(pg_constraint.conkey, 1) = 1
            """
        )
    ).all()
    found = {
        (_validated_identifier(table_name), _validated_identifier(column_name))
        for table_name, _constraint_name, column_name in rows
    }
    if found != _PERSON_REFERENCE_COLUMNS:
        raise RuntimeError(
            "Refusing to change person foreign keys because they are not "
            "the expected requirement and evidence references"
        )
    return tuple(
        (
            _validated_identifier(table_name),
            _validated_identifier(constraint_name),
        )
        for table_name, constraint_name, _column_name in rows
    )


def _current_schema(bind: sa.Connection) -> str:
    if op.get_context().as_sql:
        return "public"
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    if not isinstance(schema, str) or schema.lower() in {
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
    }:
        raise RuntimeError("refusing to enable row-level security in this schema")
    return _validated_identifier(schema)


def _require_privileged_migration_connection(bind: sa.Connection) -> None:
    privileged = bind.execute(
        sa.text(
            """
            SELECT rolsuper OR rolbypassrls
            FROM pg_roles
            WHERE rolname = current_user
            """
        )
    ).scalar()
    if privileged:
        return
    owns_workflows = bind.execute(
        sa.text(
            """
            SELECT pg_get_userbyid(pg_class.relowner) = current_user
            FROM pg_class
            JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
            WHERE pg_class.relname = 'workflows'
              AND pg_namespace.nspname = current_schema()
            """
        )
    ).scalar()
    if not owns_workflows:
        raise RuntimeError(
            "Refusing to downgrade row-level security without a privileged "
            "migration connection"
        )


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
        raise RuntimeError(REFUSAL)


def _validated_identifier(identifier: str) -> str:
    if not identifier or not identifier.replace("_", "").isalnum():
        raise RuntimeError("refusing an unexpected schema name")
    if identifier[0].isdigit():
        raise RuntimeError("refusing an unexpected schema name")
    return identifier


def _quote(identifier: str) -> str:
    return '"' + _validated_identifier(identifier).replace('"', "") + '"'
