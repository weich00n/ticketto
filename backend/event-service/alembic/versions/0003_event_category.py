# pylint: disable=no-member,invalid-name
"""Event category.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: events made before this have no category.
    op.add_column("events", sa.Column("category", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("events", "category")
