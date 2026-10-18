"""Create accountability persistence and append-only audit tables.

Revision ID: 20261009_01
Revises:
Create Date: 2026-10-09
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261009_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "people",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=200), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_people_email", "people", ["email"])

    op.create_table(
        "workflows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("workflow_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.CheckConstraint(
            "workflow_type IN ('grant', 'compliance', 'vendor_contract', "
            "'customer_implementation', 'regulatory_reporting', 'general')",
            name="ck_workflows_type",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'completed', 'cancelled')",
            name="ck_workflows_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflows_type_status",
        "workflows",
        ["workflow_type", "status"],
    )

    op.create_table(
        "requirements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("supervisor_id", sa.Uuid(), nullable=True),
        sa.Column("evidence_expectation", sa.Text(), nullable=False),
        sa.Column("provenance", sa.String(length=40), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("target", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("review_status", sa.String(length=30), nullable=False),
        sa.Column("reviewed_by_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "provenance IN ('organization_template', 'document_explicit', "
            "'ai_inferred', 'user_provided', 'system_derived')",
            name="ck_requirements_provenance",
        ),
        sa.CheckConstraint(
            "status IN ('not_started', 'in_progress', 'blocked', "
            "'completed', 'cancelled')",
            name="ck_requirements_status",
        ),
        sa.CheckConstraint(
            "review_status IN ('pending_review', 'approved', 'rejected')",
            name="ck_requirements_review_status",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["people.id"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_id"],
            ["people.id"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["supervisor_id"],
            ["people.id"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_requirements_due_date",
        "requirements",
        ["due_date"],
    )
    op.create_index(
        "ix_requirements_workflow_status",
        "requirements",
        ["workflow_id", "status"],
    )

    op.create_table(
        "evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("requirement_id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("evidence_type", sa.String(length=30), nullable=False),
        sa.Column("submitted_by_id", sa.Uuid(), nullable=False),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("reference", sa.String(length=1000), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.CheckConstraint(
            "evidence_type IN ('document', 'link', 'note', 'record')",
            name="ck_evidence_type",
        ),
        sa.CheckConstraint(
            "status IN ('submitted')",
            name="ck_evidence_status",
        ),
        sa.ForeignKeyConstraint(
            ["requirement_id"],
            ["requirements.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by_id"],
            ["people.id"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_evidence_requirement",
        "evidence",
        ["requirement_id"],
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("before_state", sa.JSON(), nullable=True),
        sa.Column("after_state", sa.JSON(), nullable=False),
        sa.Column("actor_reference", sa.String(length=255), nullable=True),
        sa.Column("correlation_id", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "length(actor_reference) <= 255",
            name="ck_audit_actor_reference_length",
        ),
        sa.CheckConstraint(
            "length(correlation_id) <= 255",
            name="ck_audit_correlation_id_length",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_entity_history",
        "audit_events",
        ["entity_type", "entity_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_entity_history", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_evidence_requirement", table_name="evidence")
    op.drop_table("evidence")
    op.drop_index(
        "ix_requirements_workflow_status",
        table_name="requirements",
    )
    op.drop_index("ix_requirements_due_date", table_name="requirements")
    op.drop_table("requirements")
    op.drop_index("ix_workflows_type_status", table_name="workflows")
    op.drop_table("workflows")
    op.drop_index("ix_people_email", table_name="people")
    op.drop_table("people")
