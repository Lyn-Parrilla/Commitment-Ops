"""Persist requirement review decisions and learning signals.

Revision ID: 20261018_05
Revises: 20261017_04
Create Date: 2026-10-18

Candidates, review decisions, and learning signals are separate records.
Decisions and signals are insert-only. A decision or signal that names a
candidate or decision must name the same organization. Human-added signals
may leave the candidate unset. PostgreSQL forces row-level security on the
new tables and grants the application role no update on those immutable
records. SQLite creates the tables and skips policies.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from alembic import op
import sqlalchemy as sa

revision: str = "20261018_05"
down_revision: str | None = "20261017_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "commitmentops_app"
CANDIDATES = "requirement_candidates"
DECISIONS = "candidate_review_decisions"
SIGNALS = "learning_signals"
TABLES = (CANDIDATES, DECISIONS, SIGNALS)
LEGACY_ORGANIZATION_ID = UUID("00000000-0000-0000-0000-000000000001")
POPULATED = (
    "Refusing to downgrade while review decisions or learning signals exist. "
    "Export those records before downgrading."
)
TENANT_POPULATED = (
    "Refusing to downgrade while non-legacy organizations, "
    "organization memberships, or non-legacy workflow assignments "
    "exist. Export or deliberately remove those tenant records "
    "before downgrading."
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
    }
)


def upgrade() -> None:
    _create_tables()
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    schema = _current_schema(bind)
    quoted_schema = _quote(schema)
    for table, privileges in (
        (CANDIDATES, "SELECT, INSERT, UPDATE"),
        (DECISIONS, "SELECT, INSERT"),
        (SIGNALS, "SELECT, INSERT"),
    ):
        bind.execute(
            sa.text(
                f"GRANT {privileges} ON {quoted_schema}.{_quote(table)} "
                f"TO {APP_ROLE}"
            )
        )
    for table in TABLES:
        qualified = f"{quoted_schema}.{_quote(table)}"
        bind.execute(
            sa.text(
                f"""
                CREATE POLICY {table}_tenant ON {qualified}
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
                f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY"
            )
        )
        bind.execute(
            sa.text(
                f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not op.get_context().as_sql:
        _refuse_populated_tenant_downgrade(bind)
        populated = bind.execute(
            sa.text(
                f"""
                SELECT
                    (SELECT COUNT(*) FROM {CANDIDATES})
                    + (SELECT COUNT(*) FROM {DECISIONS})
                    + (SELECT COUNT(*) FROM {SIGNALS})
                """
            )
        ).scalar()
        if populated:
            raise RuntimeError(POPULATED)
    if bind.dialect.name == "postgresql":
        schema = _current_schema(bind)
        quoted_schema = _quote(schema)
        for table in TABLES:
            qualified = f"{quoted_schema}.{_quote(table)}"
            bind.execute(sa.text(f"DROP POLICY IF EXISTS {table}_tenant ON {qualified}"))
            bind.execute(
                sa.text(f"ALTER TABLE {qualified} NO FORCE ROW LEVEL SECURITY")
            )
            bind.execute(
                sa.text(f"ALTER TABLE {qualified} DISABLE ROW LEVEL SECURITY")
            )
    op.drop_table(SIGNALS)
    op.drop_table(DECISIONS)
    op.drop_table(CANDIDATES)


def _create_tables() -> None:
    op.create_table(
        CANDIDATES,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("provenance", sa.String(length=40), nullable=False),
        sa.Column("classification", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("target", sa.String(length=500), nullable=True),
        sa.Column("evidence_expectation", sa.Text(), nullable=True),
        sa.Column("owner_snapshot", sa.JSON(), nullable=True),
        sa.Column("supervisor_snapshot", sa.JSON(), nullable=True),
        sa.Column("discovery_reason", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source_document", sa.JSON(), nullable=False),
        sa.Column("source_location", sa.JSON(), nullable=True),
        sa.Column("original_discovery", sa.JSON(), nullable=False),
        sa.Column("review_status", sa.String(length=30), nullable=False),
        sa.Column("reviewed_by_snapshot", sa.JSON(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "provenance IN ('organization_template', 'document_explicit', "
            "'ai_inferred', 'user_provided', 'system_derived')",
            name="ck_requirement_candidates_provenance",
        ),
        sa.CheckConstraint(
            "classification IN ('requirement', 'deliverable')",
            name="ck_requirement_candidates_classification",
        ),
        sa.CheckConstraint(
            "review_status IN ('pending_review', 'approved', 'rejected')",
            name="ck_requirement_candidates_review_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_requirement_candidates_organization_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            name="uq_requirement_candidates_id_organization_id",
        ),
    )
    op.create_index(
        "ix_requirement_candidates_organization_id",
        CANDIDATES,
        ["organization_id"],
    )
    op.create_table(
        DECISIONS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("human_decision", sa.String(length=20), nullable=False),
        sa.Column("reviewer_user_id", sa.Uuid(), nullable=False),
        sa.Column("reviewer_snapshot", sa.JSON(), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "human_decision IN ('approved', 'rejected', 'human_added')",
            name="ck_candidate_review_decisions_human_decision",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_candidate_review_decisions_organization_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id", "organization_id"],
            ["requirement_candidates.id", "requirement_candidates.organization_id"],
            name="fk_candidate_review_decisions_candidate_organization",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            name="uq_candidate_review_decisions_id_organization_id",
        ),
    )
    op.create_index(
        "ix_candidate_review_decisions_organization_id",
        DECISIONS,
        ["organization_id"],
    )
    op.create_index(
        "ix_candidate_review_decisions_candidate_id",
        DECISIONS,
        ["candidate_id"],
    )
    op.create_table(
        SIGNALS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("source_document", sa.JSON(), nullable=False),
        sa.Column("source_location", sa.JSON(), nullable=True),
        sa.Column("original_discovery", sa.JSON(), nullable=True),
        sa.Column("reviewed_candidate", sa.JSON(), nullable=True),
        sa.Column("discovery_provenance", sa.String(length=40), nullable=True),
        sa.Column("discovery_reason", sa.Text(), nullable=True),
        sa.Column("original_confidence", sa.Float(), nullable=True),
        sa.Column("resulting_requirement", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "discovery_provenance IS NULL OR discovery_provenance IN "
            "('organization_template', 'document_explicit', 'ai_inferred', "
            "'user_provided', 'system_derived')",
            name="ck_learning_signals_discovery_provenance",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_learning_signals_organization_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["decision_id", "organization_id"],
            [
                "candidate_review_decisions.id",
                "candidate_review_decisions.organization_id",
            ],
            name="fk_learning_signals_decision_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id", "organization_id"],
            [
                "requirement_candidates.id",
                "requirement_candidates.organization_id",
            ],
            name="fk_learning_signals_candidate_organization",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("decision_id", name="uq_learning_signals_decision_id"),
    )
    op.create_index(
        "ix_learning_signals_organization_id",
        SIGNALS,
        ["organization_id"],
    )
    op.create_index(
        "ix_learning_signals_candidate_id",
        SIGNALS,
        ["candidate_id"],
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
        raise RuntimeError(TENANT_POPULATED)


def _current_schema(bind: sa.Connection) -> str:
    if op.get_context().as_sql:
        return "public"
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    if not isinstance(schema, str) or schema.lower() in _DENIED_SCHEMAS:
        raise RuntimeError("refusing to enable row-level security in this schema")
    return _validated_identifier(schema)


def _validated_identifier(identifier: str) -> str:
    if not identifier or not identifier.replace("_", "").isalnum():
        raise RuntimeError("refusing an unexpected schema name")
    if identifier[0].isdigit():
        raise RuntimeError("refusing an unexpected schema name")
    return identifier


def _quote(identifier: str) -> str:
    return '"' + _validated_identifier(identifier).replace('"', "") + '"'
