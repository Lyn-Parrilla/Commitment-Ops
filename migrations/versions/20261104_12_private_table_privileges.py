"""Revoke Supabase API-role privileges on private tables.

Revision ID: 20261104_12
Revises: 20261103_11
Create Date: 2026-11-04

Supabase can grant ``anon``, ``authenticated``, and ``service_role`` direct
table privileges. Revoking ``PUBLIC`` leaves those grants in place. This
revision removes every table privilege those roles hold on the private
evidence tables and on ``source_documents``.

``commitmentops_app`` and ``commitmentops_submission`` keep the grants from
earlier revisions. ``commitmentops_maintenance`` receives no table grant.
Role memberships, function ownership, security-definer functions, and
row-level security policies are unchanged.

Downgrade does not grant any privilege back to ``anon``,
``authenticated``, or ``service_role``.

SQLite has no such roles. Its upgrade changes nothing. When a downgrade
command will also run revision 09, this revision refuses before that
revision runs, so a failed SQLite command leaves the stamp here.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from uuid import UUID

from alembic import op
import sqlalchemy as sa

revision: str = "20261104_12"
down_revision: str | None = "20261103_11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "evidence_files",
    "evidence_file_reservations",
    "evidence_storage_cleanup",
    "source_documents",
)
_API_ROLES = ("anon", "authenticated", "service_role")
_INTERNAL_ROLES = (
    "commitmentops_app",
    "commitmentops_submission",
    "commitmentops_maintenance",
)
_PREDECESSOR = "20261027_09"
_FILES = "evidence_files"
_LEGACY_ORGANIZATION_ID = UUID("00000000-0000-0000-0000-000000000001")
_EVIDENCE_POPULATED = (
    "refusing to downgrade while evidence files exist. "
    "Remove those private files through the application first."
)
_TENANT_POPULATED = (
    "Refusing to downgrade while non-legacy organizations, "
    "organization memberships, or non-legacy workflow assignments "
    "exist. Export or deliberately remove those tenant records "
    "before downgrading."
)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
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
    schema = _current_schema(bind)
    bind.execute(sa.text(private_table_revoke_sql(schema)))


def downgrade() -> None:
    """Keep the API-role revokes. Do not grant those roles anything.

    SQLite commits each revision. This revision's downgrade changes no
    schema there, so a later refusal would leave the stamp at
    ``20261103_11`` after a failed command. Refuse first, using the same
    tenant and evidence checks, when revision 09 will run in this command.
    """

    bind = op.get_bind()
    if not op.get_context().as_sql and _downgrade_includes(_PREDECESSOR):
        _refuse_blocked_downgrade(bind)


def private_table_revoke_sql(schema: str) -> str:
    """SQL that fails closed, then revokes API-role table privileges."""

    schema = _validated_identifier(schema)
    if schema in _DENIED_SCHEMAS:
        raise RuntimeError("database schema is not valid")
    checks = []
    for table in _TABLES:
        checks.append(
            f"""
            IF to_regclass('{schema}.{table}') IS NULL THEN
                RAISE EXCEPTION 'required table {table} is missing';
            END IF;
            """
        )
    for role in _INTERNAL_ROLES:
        checks.append(
            f"""
            IF NOT EXISTS (
                SELECT 1
                  FROM pg_catalog.pg_roles
                 WHERE rolname = '{role}'
            ) THEN
                RAISE EXCEPTION 'required role {role} is missing';
            END IF;
            """
        )
    revokes = []
    for role in _API_ROLES:
        statements = "\n".join(
            "EXECUTE 'REVOKE ALL ON TABLE "
            f"{_quote(schema)}.{_quote(table)} FROM {_quote(role)}';"
            for table in _TABLES
        )
        revokes.append(
            f"""
            IF EXISTS (
                SELECT 1
                  FROM pg_catalog.pg_roles
                 WHERE rolname = '{role}'
            ) THEN
                {statements}
            END IF;
            """
        )
    body = "\n".join(checks + revokes)
    return (
        "DO $revoke_api_table_privileges$\n"
        "BEGIN\n"
        f"{body}\n"
        "END\n"
        "$revoke_api_table_privileges$;"
    )


def _downgrade_includes(target: str) -> bool:
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
        getattr(getattr(step, "revision", None), "revision", None) == target
        for step in steps
    )


def _refuse_blocked_downgrade(bind: sa.Connection) -> None:
    populated = bind.execute(sa.text(f"SELECT COUNT(*) FROM {_FILES}")).scalar()
    if populated:
        raise RuntimeError(_EVIDENCE_POPULATED)
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


def _current_schema(bind: sa.Connection) -> str:
    if op.get_context().as_sql:
        return "public"
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    if (
        not isinstance(schema, str)
        or _IDENTIFIER.fullmatch(schema) is None
        or schema in _DENIED_SCHEMAS
    ):
        raise RuntimeError("database schema is not valid")
    return schema


def _validated_identifier(identifier: str) -> str:
    if _IDENTIFIER.fullmatch(identifier) is None:
        raise RuntimeError("database identifier is not valid")
    return identifier


def _quote(identifier: str) -> str:
    return '"' + _validated_identifier(identifier) + '"'
