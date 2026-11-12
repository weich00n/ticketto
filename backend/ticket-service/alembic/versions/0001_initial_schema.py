# pylint: disable=invalid-name,no-member
"""Initial ticket-service schema

Revision ID: 0001
Revises:
Create Date: 2026-10-19
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _ts(name: str, nullable: bool = True, server_default=None) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=nullable, server_default=server_default
    )


def upgrade() -> None:
    op.create_table(
        "event_states",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("event_version", sa.Integer()),
        sa.Column("title", sa.String(160)),
        sa.Column("venue", sa.String(200)),
        _ts("starts_at"),
        sa.Column("category_names", sa.JSON()),
        sa.Column("cancellation_id", sa.String(64)),
        _ts("cancelled_at"),
        sa.Column("cancellation_reason", sa.String(500)),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "ticket_orders",
        sa.Column("order_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("category_id", sa.String(36), nullable=False),
        sa.Column("hold_id", sa.String(36), nullable=False),
        sa.Column("admission_id", sa.String(36), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("delivery_email", sa.String(320)),
        sa.Column("issuance_status", sa.String(16), nullable=False),
        sa.Column("rejection_reason", sa.String(32)),
        _ts("confirmed_at"),
        sa.Column("source_message_id", sa.String(64), nullable=False),
        _ts("received_at", nullable=False),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ticket_orders_user_id", "ticket_orders", ["user_id"])
    op.create_index("ix_ticket_orders_event_id", "ticket_orders", ["event_id"])

    op.create_table(
        "tickets",
        sa.Column("ticket_id", sa.String(36), primary_key=True),
        sa.Column(
            "order_id",
            sa.String(36),
            sa.ForeignKey("ticket_orders.order_id", name="fk_tickets_order"),
            nullable=False,
        ),
        sa.Column("ticket_number", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("category_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("qr_key_id", sa.String(16), nullable=False),
        sa.Column("qr_signature", sa.String(64), nullable=False),
        _ts("issued_at", nullable=False),
        _ts("checked_in_at"),
        sa.Column("checked_in_by", sa.String(128)),
        _ts("invalidated_at"),
        sa.Column("invalidation_reason", sa.String(32)),
        sa.UniqueConstraint("order_id", "ticket_number", name="uq_tickets_order_number"),
    )
    op.create_index("ix_tickets_user_issued", "tickets", ["user_id", "issued_at"])
    op.create_index("ix_tickets_event_status", "tickets", ["event_id", "status"])

    op.create_table(
        "check_ins",
        sa.Column("check_in_id", sa.String(36), primary_key=True),
        sa.Column(
            "ticket_id",
            sa.String(36),
            sa.ForeignKey("tickets.ticket_id", name="fk_check_ins_ticket"),
            nullable=False,
            unique=True,
        ),
        sa.Column("event_id", sa.String(36), nullable=False),
        _ts("checked_in_at", nullable=False),
        sa.Column("checked_in_by", sa.String(128)),
    )
    op.create_index("ix_check_ins_event_id", "check_ins", ["event_id"])

    op.create_table(
        "order_refunds",
        sa.Column("order_id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("refund_id", sa.String(64)),
        sa.Column("refund_amount", sa.BigInteger()),
        sa.Column("currency", sa.String(3)),
        _ts("completed_at"),
        _ts("received_at", nullable=False),
    )

    op.create_table(
        "email_deliveries",
        sa.Column(
            "order_id",
            sa.String(36),
            sa.ForeignKey("ticket_orders.order_id", name="fk_email_deliveries_order"),
            primary_key=True,
        ),
        sa.Column("recipient", sa.String(320)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        _ts("next_attempt_at"),
        _ts("last_attempt_at"),
        _ts("sent_at"),
        sa.Column("last_error", sa.Text()),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_email_deliveries_due", "email_deliveries", ["status", "next_attempt_at"])

    op.create_table(
        "processed_messages",
        sa.Column("message_id", sa.String(64), primary_key=True),
        sa.Column("message_type", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        _ts("processed_at", nullable=False),
    )
    op.create_index("ix_processed_messages_processed_at", "processed_messages", ["processed_at"])


def downgrade() -> None:
    op.drop_table("processed_messages")
    op.drop_table("email_deliveries")
    op.drop_table("order_refunds")
    op.drop_table("check_ins")
    op.drop_table("tickets")
    op.drop_table("ticket_orders")
    op.drop_table("event_states")
