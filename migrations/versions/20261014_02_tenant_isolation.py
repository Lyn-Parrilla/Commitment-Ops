"""Add organizations, memberships, and workflow tenant ownership.

Revision ID: 20261014_02
Revises: 20261009_01
Create Date: 2026-10-14
"""

from collections.abc import Sequence
from uuid import UUID

from alembic import op
import sqlalchemy as sa

revision: str = "20261014_02"
down_revision: str | None = "20261009_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_ORGANIZATION_ID = UUID("00000000-0000-0000-0000-000000000001")


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "organization_memberships",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("organization_id", "user_id"),
    )
    op.create_index(
        "ix_organization_memberships_user_active",
        "organization_memberships",
        ["user_id", "is_active"],
    )

    organizations = sa.table(
        "organizations",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
    )
    op.bulk_insert(
        organizations,
        [{"id": LEGACY_ORGANIZATION_ID, "name": "Legacy Unassigned"}],
    )

    with op.batch_alter_table("workflows") as batch_op:
        batch_op.add_column(
            sa.Column("organization_id", sa.Uuid(), nullable=True)
        )
    workflows = sa.table(
        "workflows",
        sa.column("organization_id", sa.Uuid()),
    )
    op.execute(
        sa.update(workflows)
        .where(workflows.c.organization_id.is_(None))
        .values(organization_id=LEGACY_ORGANIZATION_ID)
    )
    with op.batch_alter_table("workflows") as batch_op:
        batch_op.alter_column(
            "organization_id",
            existing_type=sa.Uuid(),
            nullable=False,
        )
        batch_op.create_foreign_key(
            "fk_workflows_organization_id_organizations",
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(
            "ix_workflows_organization_id",
            ["organization_id"],
        )


def downgrade() -> None:
    connection = op.get_bind()
    workflows = sa.table(
        "workflows",
        sa.column("organization_id", sa.Uuid()),
    )
    memberships = sa.table(
        "organization_memberships",
        sa.column("organization_id", sa.Uuid()),
    )
    assigned_workflows = connection.scalar(
        sa.select(sa.func.count())
        .select_from(workflows)
        .where(workflows.c.organization_id != LEGACY_ORGANIZATION_ID)
    )
    membership_count = connection.scalar(
        sa.select(sa.func.count()).select_from(memberships)
    )
    if assigned_workflows or membership_count:
        raise RuntimeError(
            "Refusing to downgrade while organization memberships or "
            "non-legacy workflow assignments exist. Export or deliberately "
            "remove those tenant assignments before downgrading."
        )

    with op.batch_alter_table("workflows") as batch_op:
        batch_op.drop_index("ix_workflows_organization_id")
        batch_op.drop_constraint(
            "fk_workflows_organization_id_organizations",
            type_="foreignkey",
        )
        batch_op.drop_column("organization_id")
    op.drop_index(
        "ix_organization_memberships_user_active",
        table_name="organization_memberships",
    )
    op.drop_table("organization_memberships")
    op.drop_table("organizations")
