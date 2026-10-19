"""Persist assistance proposals and immutable lifecycle events.

Revision ID: 20261018_06
Revises: 20261018_05
Create Date: 2026-10-18

Proposals cite a workflow and requirement in the same organization. One open
proposal is allowed for each requirement and recommendation type. Lifecycle
events are insert-only. PostgreSQL forces row-level security and grants the
application role no delete, and update only of the proposal state. SQLite
creates the tables and skips policies.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from alembic import op
import sqlalchemy as sa

revision: str = "20261018_06"
down_revision: str | None = "20261018_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "commitmentops_app"
PROPOSALS = "assistance_proposals"
EVENTS = "assistance_proposal_events"
TABLES = (PROPOSALS, EVENTS)
LEGACY_ORGANIZATION_ID = UUID("00000000-0000-0000-0000-000000000001")
POPULATED = (
    "Refusing to downgrade while assistance proposals or lifecycle events "
    "exist. Export those records before downgrading."
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
    _prepare_references()
    _create_tables()
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    schema = _current_schema(bind)
    quoted_schema = _quote(schema)
    quoted_proposals = f"{quoted_schema}.{_quote(PROPOSALS)}"
    bind.execute(
        sa.text(
            f"GRANT SELECT, INSERT ON {quoted_proposals} TO {APP_ROLE}"
        )
    )
    bind.execute(
        sa.text(
            f"GRANT UPDATE (lifecycle_state) ON {quoted_proposals} TO {APP_ROLE}"
        )
    )
    bind.execute(
        sa.text(
            f"GRANT SELECT, INSERT ON {quoted_schema}.{_quote(EVENTS)} "
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
        bind.execute(sa.text(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY"))
        bind.execute(sa.text(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY"))


def downgrade() -> None:
    bind = op.get_bind()
    if not op.get_context().as_sql:
        _refuse_populated_tenant_downgrade(bind)
        populated = bind.execute(
            sa.text(
                f"""
                SELECT
                    (SELECT COUNT(*) FROM {PROPOSALS})
                    + (SELECT COUNT(*) FROM {EVENTS})
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
            bind.execute(
                sa.text(f"DROP POLICY IF EXISTS {table}_tenant ON {qualified}")
            )
            bind.execute(sa.text(f"ALTER TABLE {qualified} NO FORCE ROW LEVEL SECURITY"))
            bind.execute(sa.text(f"ALTER TABLE {qualified} DISABLE ROW LEVEL SECURITY"))
    op.drop_table(EVENTS)
    op.drop_table(PROPOSALS)
    with op.batch_alter_table("requirements") as batch_op:
        batch_op.drop_constraint(
            "uq_requirements_id_workflow_id",
            type_="unique",
        )
    with op.batch_alter_table("workflows") as batch_op:
        batch_op.drop_constraint(
            "uq_workflows_id_organization_id",
            type_="unique",
        )


def _prepare_references() -> None:
    with op.batch_alter_table("workflows") as batch_op:
        batch_op.create_unique_constraint(
            "uq_workflows_id_organization_id",
            ["id", "organization_id"],
        )
    with op.batch_alter_table("requirements") as batch_op:
        batch_op.create_unique_constraint(
            "uq_requirements_id_workflow_id",
            ["id", "workflow_id"],
        )


def _create_tables() -> None:
    op.create_table(
        PROPOSALS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("requirement_id", sa.Uuid(), nullable=False),
        sa.Column("recommendation_type", sa.String(length=40), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("intended_outcome", sa.Text(), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "recommendation_type IN ('overdue_requirement', 'blocked_requirement')",
            name="ck_assistance_proposals_recommendation_type",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('proposed', 'approved', 'rejected', "
            "'dismissed', 'executed')",
            name="ck_assistance_proposals_lifecycle_state",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_assistance_proposals_organization_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id", "organization_id"],
            ["workflows.id", "workflows.organization_id"],
            name="fk_assistance_proposals_workflow_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requirement_id", "workflow_id"],
            ["requirements.id", "requirements.workflow_id"],
            name="fk_assistance_proposals_requirement_workflow",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            name="uq_assistance_proposals_id_organization_id",
        ),
    )
    op.create_index(
        "ix_assistance_proposals_organization_id",
        PROPOSALS,
        ["organization_id"],
    )
    op.create_index(
        "ix_assistance_proposals_workflow_id",
        PROPOSALS,
        ["workflow_id"],
    )
    op.create_index(
        "uq_assistance_proposals_open_condition",
        PROPOSALS,
        [
            "organization_id",
            "workflow_id",
            "requirement_id",
            "recommendation_type",
        ],
        unique=True,
        postgresql_where=sa.text(
            "lifecycle_state IN ('proposed', 'approved')"
        ),
        sqlite_where=sa.text(
            "lifecycle_state IN ('proposed', 'approved')"
        ),
    )
    op.create_table(
        EVENTS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('proposed', 'approved', 'rejected', 'dismissed', "
            "'executed', 'outcome_recorded')",
            name="ck_assistance_proposal_events_event_type",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_assistance_proposal_events_organization_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id", "organization_id"],
            ["assistance_proposals.id", "assistance_proposals.organization_id"],
            name="fk_assistance_proposal_events_proposal_organization",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "proposal_id",
            "event_type",
            name="uq_assistance_proposal_events_proposal_type",
        ),
    )
    op.create_index(
        "ix_assistance_proposal_events_organization_id",
        EVENTS,
        ["organization_id"],
    )
    op.create_index(
        "ix_assistance_proposal_events_proposal_id",
        EVENTS,
        ["proposal_id"],
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
