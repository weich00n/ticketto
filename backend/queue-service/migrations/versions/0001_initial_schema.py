"""Initial schema: queue_configs and processed_messages.

Revision ID: 0001
Revises:
Create Date: 2026-10-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = ("SCHEDULED", "OPEN", "ADMITTING", "CLOSED", "CANCELLED")


def upgrade() -> None:
    op.create_table(
        "queue_configs",
        sa.Column("event_id", sa.CHAR(36), primary_key=True),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column("total_capacity", sa.Integer(), nullable=False),
        sa.Column("active_pool_capacity", sa.Integer(), nullable=False),
        sa.Column("queue_opens_at", mysql.DATETIME(fsp=3), nullable=False),
        sa.Column("release_at", mysql.DATETIME(fsp=3), nullable=False),
        sa.Column("status", sa.Enum(*STATUSES, name="status"), nullable=False),
        sa.Column("closed_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=3),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=3),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)"),
        ),
    )
    op.create_index("idx_status_release", "queue_configs", ["status", "release_at"])

    op.create_table(
        "processed_messages",
        sa.Column("message_id", sa.CHAR(36), primary_key=True),
        sa.Column("message_type", sa.String(64), nullable=False),
        sa.Column(
            "processed_at",
            mysql.DATETIME(fsp=3),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
        ),
    )


def downgrade() -> None:
    op.drop_table("processed_messages")
    op.drop_index("idx_status_release", table_name="queue_configs")
    op.drop_table("queue_configs")
