"""Persist private grant and contract documents for one organization.

Revision ID: 20261103_11
Revises: 20261028_10
Create Date: 2026-11-03

Document bytes stay in private object storage. This table stores metadata,
processing status, and extracted page text for human review. PostgreSQL
forces row-level security and grants the application role no delete.
SQLite creates the table and skips policies.

Downgrade refuses while source documents exist. When the same command will
also run revision 09, it refuses before changing this table so a failed
SQLite command leaves the stamp at this revision.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from uuid import UUID

from alembic import op
import sqlalchemy as sa

revision: str = "20261103_11"
down_revision: str | None = "20261028_10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "source_documents"
_APP_ROLE = "commitmentops_app"
_PREDECESSOR = "20261027_09"
_FILES = "evidence_files"
_LEGACY_ORGANIZATION_ID = UUID("00000000-0000-0000-0000-000000000001")
_POPULATED = (
    "refusing to downgrade while source documents exist. "
    "Remove those private files through the application first."
)
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
    }
)


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(length=200), nullable=False),
        sa.Column("media_type", sa.String(length=120), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("document_type", sa.String(length=40), nullable=False),
        sa.Column("storage_key", sa.String(length=118), nullable=False),
        sa.Column("processing_status", sa.String(length=20), nullable=False),
        sa.Column("failure_code", sa.String(length=40), nullable=True),
        sa.Column(
            "text_truncated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("extracted_pages", sa.JSON(), nullable=True),
        sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "document_type IN ('grant_agreement', 'contract', 'policy', "
            "'regulatory_filing', 'implementation_plan', 'other')",
            name="ck_source_documents_document_type",
        ),
        sa.CheckConstraint(
            "processing_status IN ('uploaded', 'processing', 'processed', 'failed')",
            name="ck_source_documents_processing_status",
        ),
        sa.CheckConstraint(
            "byte_size BETWEEN 1 AND 10485760",
            name="ck_source_documents_byte_size",
        ),
        sa.CheckConstraint(
            "length(checksum_sha256) = 64",
            name="ck_source_documents_checksum",
        ),
        sa.CheckConstraint(
            "length(storage_key) = 118 AND storage_key LIKE 'sources/%'",
            name="ck_source_documents_storage_key",
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR failure_code IN ("
            "'no_extractable_text', 'interpretation_unavailable', "
            "'interpretation_invalid', 'interpretation_failed')",
            name="ck_source_documents_failure_code",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_source_documents_organization_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id", "organization_id"],
            ["workflows.id", "workflows.organization_id"],
            name="fk_source_documents_workflow_organization",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "workflow_id",
            "checksum_sha256",
            name="uq_source_documents_workflow_checksum",
        ),
    )
    op.create_index(
        "ix_source_documents_organization_id",
        _TABLE,
        ["organization_id"],
    )
    op.create_index(
        "ix_source_documents_workflow_id",
        _TABLE,
        ["workflow_id"],
    )
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    schema = _current_schema(bind)
    qualified = f"{_quote(schema)}.{_quote(_TABLE)}"
    bind.execute(
        sa.text(
            f"GRANT SELECT, INSERT, UPDATE ON {qualified} TO {_quote(_APP_ROLE)}"
        )
    )
    bind.execute(
        sa.text(
            f"""
            CREATE POLICY {_TABLE}_tenant ON {qualified}
            FOR ALL
            USING (
                organization_id = {_quote(schema)}.app_current_organization_id()
                AND {_quote(schema)}.app_is_active_member(organization_id)
            )
            WITH CHECK (
                organization_id = {_quote(schema)}.app_current_organization_id()
                AND {_quote(schema)}.app_is_active_member(organization_id)
            )
            """
        )
    )
    bind.execute(sa.text(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY"))
    bind.execute(sa.text(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY"))


def downgrade() -> None:
    bind = op.get_bind()
    if not op.get_context().as_sql and _downgrade_includes(_PREDECESSOR):
        _refuse_blocked_downgrade(bind)
    if not op.get_context().as_sql:
        populated = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {_TABLE}")
        ).scalar()
        if populated:
            raise RuntimeError(_POPULATED)
    if bind.dialect.name == "postgresql":
        schema = _current_schema(bind)
        qualified = f"{_quote(schema)}.{_quote(_TABLE)}"
        bind.execute(
            sa.text(f"ALTER TABLE {qualified} NO FORCE ROW LEVEL SECURITY")
        )
        bind.execute(
            sa.text(f"DROP POLICY IF EXISTS {_TABLE}_tenant ON {qualified}")
        )
    op.drop_index("ix_source_documents_workflow_id", table_name=_TABLE)
    op.drop_index("ix_source_documents_organization_id", table_name=_TABLE)
    op.drop_table(_TABLE)


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


def _quote(identifier: str) -> str:
    if _IDENTIFIER.fullmatch(identifier) is None:
        raise RuntimeError("database identifier is not valid")
    return '"' + identifier + '"'
