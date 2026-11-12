# pylint: disable=no-member,invalid-name  # alembic.op is populated at runtime; Alembic requires these names
"""Events, queued emails and processed messages

Revision ID: 0001
Revises:
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MYSQL = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(160)),
        sa.Column("starts_at", sa.DateTime()),
        sa.Column("announced_at", sa.DateTime()),
        sa.Column("cancelled_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        **MYSQL,
    )
    op.create_table(
        "notifications",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("dedupe_key", sa.String(200), nullable=False),
        sa.Column("source_message_id", sa.String(64), nullable=False),
        sa.Column("event_id", sa.String(36)),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("recipient", sa.String(320), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime()),
        sa.Column("last_attempt_at", sa.DateTime()),
        sa.Column("sent_at", sa.DateTime()),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("dedupe_key", name="uq_notifications_dedupe_key"),
        **MYSQL,
    )
    op.create_index("ix_notifications_due", "notifications", ["status", "next_attempt_at"])
    op.create_index("ix_notifications_event", "notifications", ["event_id", "kind", "status"])
    op.create_table(
        "processed_messages",
        sa.Column("message_id", sa.String(64), primary_key=True),
        sa.Column("message_type", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=False),
        **MYSQL,
    )
    op.create_index("ix_processed_messages_processed_at", "processed_messages", ["processed_at"])


def downgrade() -> None:
    op.drop_table("processed_messages")
    op.drop_table("notifications")
    op.drop_table("events")
