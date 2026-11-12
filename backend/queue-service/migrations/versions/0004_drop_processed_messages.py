"""Drop processed_messages: it was only an audit record, nothing reads it.

Duplicate deliveries stay harmless without it (event_version checks, forward-only
status rules, and the release script's not-found case).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("processed_messages")  # also drops idx_processed_at


def downgrade() -> None:
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
    op.create_index("idx_processed_at", "processed_messages", ["processed_at"])
