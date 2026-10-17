"""Add optional organization branding fields.

Revision ID: 20261016_03
Revises: 20261014_02
Create Date: 2026-10-16

Existing organization and membership rows are preserved. New columns are
nullable and have no server default.
"""

from collections.abc import Sequence
from uuid import UUID

from alembic import op
import sqlalchemy as sa

LEGACY_ORGANIZATION_ID = UUID("00000000-0000-0000-0000-000000000001")

revision: str = "20261016_03"
down_revision: str | None = "20261014_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.add_column(
            sa.Column("display_name", sa.String(length=300), nullable=True)
        )
        batch_op.add_column(
            sa.Column("logo_url", sa.String(length=2000), nullable=True)
        )
        batch_op.add_column(
            sa.Column("primary_color", sa.String(length=7), nullable=True)
        )
        batch_op.add_column(
            sa.Column("accent_color", sa.String(length=7), nullable=True)
        )
        batch_op.add_column(
            sa.Column("favicon_url", sa.String(length=2000), nullable=True)
        )


def downgrade() -> None:
    # Refuse before dropping columns so a multi-step downgrade cannot commit
    # this revision and then fail the tenant guard one step later.
    connection = op.get_bind()
    organizations = sa.table("organizations", sa.column("id", sa.Uuid()))
    workflows = sa.table(
        "workflows",
        sa.column("organization_id", sa.Uuid()),
    )
    memberships = sa.table(
        "organization_memberships",
        sa.column("organization_id", sa.Uuid()),
    )
    nonlegacy_organizations = connection.scalar(
        sa.select(sa.func.count())
        .select_from(organizations)
        .where(organizations.c.id != LEGACY_ORGANIZATION_ID)
    )
    assigned_workflows = connection.scalar(
        sa.select(sa.func.count())
        .select_from(workflows)
        .where(workflows.c.organization_id != LEGACY_ORGANIZATION_ID)
    )
    membership_count = connection.scalar(
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
        raise RuntimeError(
            "Refusing to downgrade while non-legacy organizations, "
            "organization memberships, or non-legacy workflow assignments "
            "exist. Export or deliberately remove those tenant records "
            "before downgrading."
        )

    with op.batch_alter_table("organizations") as batch_op:
        batch_op.drop_column("favicon_url")
        batch_op.drop_column("accent_color")
        batch_op.drop_column("primary_color")
        batch_op.drop_column("logo_url")
        batch_op.drop_column("display_name")
