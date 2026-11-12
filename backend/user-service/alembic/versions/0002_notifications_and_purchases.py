# pylint: disable=no-member,invalid-name  # alembic.op is populated at runtime; Alembic requires these names
"""Add the new-event email setting, purchased events, and cancelled events

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default true: existing users are opted in (new-event emails are opt-out).
    op.add_column(
        "users",
        sa.Column("notify_new_events", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column("users", sa.Column("purchased_event_ids", sa.Text(), nullable=True))
    op.create_table(
        "cancelled_events",
        sa.Column("event_id", sa.CHAR(36), primary_key=True),
        sa.Column(
            "cancelled_at",
            sa.TIMESTAMP(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade() -> None:
    op.drop_table("cancelled_events")
    op.drop_column("users", "purchased_event_ids")
    op.drop_column("users", "notify_new_events")
