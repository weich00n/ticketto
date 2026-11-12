# pylint: disable=no-member,invalid-name
"""initial checkout schema: orders, payments, refunds, idempotency, dedupe, outbox

Revision ID: 0001
Revises:
Create Date: 2026-10-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# MySQL has no single shared "now" type; the dialect renders this as now().
NOW = sa.func.now()


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("order_id", sa.String(36), primary_key=True),
        sa.Column("hold_id", sa.String(36), nullable=False, unique=True),
        sa.Column("admission_id", sa.String(36)),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("category_id", sa.String(36)),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_amount", sa.BigInteger(), nullable=False),
        sa.Column("total_amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("delivery_email", sa.String(320)),
        sa.Column(
            "booking_status", sa.String(16), nullable=False, server_default="PENDING_PAYMENT"
        ),
        sa.Column("payment_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hold_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Index("ix_orders_hold_id", "hold_id"),
        sa.Index("ix_orders_user_id", "user_id"),
        sa.Index("ix_orders_event_id", "event_id"),
        sa.Index("ix_orders_booking_status", "booking_status"),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_orders_payment_expires_at", "orders", ["payment_expires_at"])

    op.create_table(
        "payments",
        sa.Column("payment_id", sa.String(36), primary_key=True),
        sa.Column(
            "order_id",
            sa.String(36),
            sa.ForeignKey("orders.order_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("stripe_payment_intent_id", sa.String(64), unique=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("succeeded_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Index("ix_payments_status", "status"),
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "refunds",
        sa.Column("refund_id", sa.String(36), primary_key=True),
        sa.Column(
            "order_id",
            sa.String(36),
            sa.ForeignKey("orders.order_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stripe_refund_id", sa.String(64), unique=True),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(80), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="REQUESTED"),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.UniqueConstraint("order_id", "reason", name="uq_refunds_order_reason"),
        sa.Index("ix_refunds_order_id", "order_id"),
        sa.Index("ix_refunds_status", "status"),
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("order_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "processed_messages",
        sa.Column("message_id", sa.String(36), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "processed_stripe_events",
        sa.Column("stripe_event_id", sa.String(64), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "outbox_messages",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("message_id", sa.String(36), nullable=False, unique=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("routing_key", sa.String(64), nullable=False),
        sa.Column("aggregate_id", sa.String(36), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("correlation_id", sa.String(128)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Index("ix_outbox_event_type", "event_type"),
        sa.Index("ix_outbox_aggregate_id", "aggregate_id"),
        mysql_charset="utf8mb4",
    )


def downgrade() -> None:
    op.drop_table("outbox_messages")
    op.drop_table("processed_stripe_events")
    op.drop_table("processed_messages")
    op.drop_table("idempotency_keys")
    op.drop_table("refunds")
    op.drop_table("payments")
    op.drop_table("orders")
