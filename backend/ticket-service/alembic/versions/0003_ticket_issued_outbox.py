# pylint: disable=invalid-name,no-member
"""Publish TicketIssued from an outbox instead of emailing tickets

notification-service now sends the ticket email. Ticket writes an `outbox_messages` row in the
same transaction as the tickets, and the worker publishes it as TicketIssued. Emails that were
still waiting to be sent are handed over: each gets a pending TicketIssued and its
`email_deliveries` row is cancelled, so neither image sends that email twice.

Additive: `email_deliveries` stays (no longer written), so the previous image still runs
against this schema.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-03
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

HANDED_OVER = "Handed over to notification-service as TicketIssued (migration 0003)."

_email = sa.table(
    "email_deliveries",
    sa.column("order_id", sa.String),
    sa.column("status", sa.String),
    sa.column("next_attempt_at", sa.DateTime),
    sa.column("last_error", sa.Text),
)
_outbox = sa.table(
    "outbox_messages",
    sa.column("message_id", sa.String),
    sa.column("message_type", sa.String),
    sa.column("routing_key", sa.String),
    sa.column("order_id", sa.String),
    sa.column("status", sa.String),
    sa.column("attempts", sa.Integer),
    sa.column("next_attempt_at", sa.DateTime),
    sa.column("last_error", sa.Text),
    sa.column("created_at", sa.DateTime),
)


def _ts(name: str, nullable: bool = True, server_default=None) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=nullable, server_default=server_default
    )


def upgrade() -> None:
    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.String(36), nullable=False, unique=True),
        sa.Column("message_type", sa.String(64), nullable=False),
        sa.Column("routing_key", sa.String(64), nullable=False),
        sa.Column(
            "order_id",
            sa.String(36),
            sa.ForeignKey("ticket_orders.order_id", name="fk_outbox_messages_order"),
            nullable=False,
        ),
        sa.Column("correlation_id", sa.String(128)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        _ts("next_attempt_at"),
        _ts("last_attempt_at"),
        _ts("published_at"),
        sa.Column("last_error", sa.Text()),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_outbox_messages_due", "outbox_messages", ["status", "next_attempt_at"])
    op.create_index("ix_outbox_messages_order", "outbox_messages", ["order_id", "id"])

    connection = op.get_bind()
    now = datetime.now(UTC).replace(microsecond=0, tzinfo=None)  # stored in UTC
    unsent = connection.execute(
        sa.select(_email.c.order_id).where(_email.c.status == "PENDING")
    ).scalars()
    rows = [
        {
            "message_id": str(uuid.uuid4()),
            "message_type": "TicketIssued",
            "routing_key": "ticket.issued",
            "order_id": order_id,
            "status": "PENDING",
            "attempts": 0,
            "next_attempt_at": now,
            "created_at": now,
        }
        for order_id in unsent
    ]
    if rows:
        op.bulk_insert(_outbox, rows)
        connection.execute(
            sa.update(_email)
            .where(_email.c.order_id.in_([r["order_id"] for r in rows]))
            .where(_email.c.status == "PENDING")
            .values(status="CANCELLED", next_attempt_at=None, last_error=HANDED_OVER)
        )


def downgrade() -> None:
    # Emails handed over and not yet published go back to the old email worker.
    connection = op.get_bind()
    unpublished = sa.select(_outbox.c.order_id).where(_outbox.c.status == "PENDING")
    connection.execute(
        sa.update(_email)
        .where(_email.c.order_id.in_(unpublished), _email.c.last_error == HANDED_OVER)
        .values(status="PENDING", next_attempt_at=sa.func.now(), last_error=None)
    )
    # Dropping the table drops its indexes; on MySQL ix_outbox_messages_order also backs the
    # foreign key, so it can't be dropped on its own first.
    op.drop_table("outbox_messages")
