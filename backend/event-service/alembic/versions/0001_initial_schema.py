# pylint: disable=no-member,invalid-name
"""Initial Event Service schema.

Revision ID: 0001
Revises:
Create Date: 2026-10-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("organiser_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("venue", sa.String(200), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ticket_release_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("cancellation_reason", sa.String(500)),
        sa.Column("event_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_events_organiser_id", "events", ["organiser_id"])
    op.create_index("ix_events_status", "events", ["status"])

    op.create_table(
        "ticket_categories",
        sa.Column("category_id", sa.String(36), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("events.event_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("price_amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("total_quantity", sa.Integer(), nullable=False),
        sa.UniqueConstraint("event_id", "name", name="uq_ticket_categories_event_name"),
    )

    op.create_table(
        "outbox_messages",
        sa.Column("message_id", sa.String(36), primary_key=True),
        sa.Column("message_type", sa.String(64), nullable=False),
        sa.Column("routing_key", sa.String(64), nullable=False),
        sa.Column("aggregate_id", sa.String(36), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("correlation_id", sa.String(128)),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
    )
    op.create_index("ix_outbox_messages_message_type", "outbox_messages", ["message_type"])
    op.create_index("ix_outbox_messages_aggregate_id", "outbox_messages", ["aggregate_id"])


def downgrade() -> None:
    op.drop_table("outbox_messages")
    op.drop_table("ticket_categories")
    op.drop_table("events")
