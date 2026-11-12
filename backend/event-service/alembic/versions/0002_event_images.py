# pylint: disable=no-member,invalid-name
"""Event images.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "events", sa.Column("image_version", sa.Integer(), nullable=False, server_default="0")
    )
    op.create_table(
        "event_images",
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("events.event_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("content_type", sa.String(32), nullable=False),
        sa.Column(
            "data", sa.LargeBinary().with_variant(mysql.MEDIUMBLOB(), "mysql"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("event_images")
    op.drop_column("events", "image_version")
