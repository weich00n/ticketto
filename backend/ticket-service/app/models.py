from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

# Ticket status
VALID = "VALID"
CHECKED_IN = "CHECKED_IN"
INVALIDATED = "INVALIDATED"
TICKET_STATUSES = (VALID, CHECKED_IN, INVALIDATED)

# Why a ticket was invalidated, or why an order was rejected
EVENT_CANCELLED = "EVENT_CANCELLED"
ORDER_REFUNDED = "ORDER_REFUNDED"

# Local event state
EVENT_ACTIVE = "ACTIVE"
EVENT_STATE_CANCELLED = "CANCELLED"

# Order issuance status
ISSUED = "ISSUED"
ORDER_INVALIDATED = "INVALIDATED"
REJECTED = "REJECTED"

# Outbox message status
OUTBOX_PENDING = "PENDING"  # not published yet: due, being published, or waiting to retry
OUTBOX_PUBLISHED = "PUBLISHED"  # the broker confirmed it
OUTBOX_CANCELLED = "CANCELLED"  # the order's tickets were invalidated before it went out
OUTBOX_STATUSES = (OUTBOX_PENDING, OUTBOX_PUBLISHED, OUTBOX_CANCELLED)

# The one message Ticket publishes (rabbitmq-service topology/messages.yaml)
TICKET_ISSUED = "TicketIssued"
TICKET_ISSUED_ROUTING_KEY = "ticket.issued"


class EventState(Base):
    """What Ticket knows about an event: cancellation, and a display snapshot.

    The row is also the per-event lock: confirmation, cancellation and refund messages
    for one event lock it, so they apply one after another."""

    __tablename__ = "event_states"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=EVENT_ACTIVE)
    event_version: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(String(160))
    venue: Mapped[str | None] = mapped_column(String(200))
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # {category_id: name}
    category_names: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    cancellation_id: Mapped[str | None] = mapped_column(String(64))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TicketOrder(Base):
    """One confirmed order as received in OrderConfirmed."""

    __tablename__ = "ticket_orders"

    order_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    category_id: Mapped[str] = mapped_column(String(36), nullable=False)
    hold_id: Mapped[str | None] = mapped_column(String(36))  # recorded only; may be absent
    admission_id: Mapped[str | None] = mapped_column(String(36))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    delivery_email: Mapped[str | None] = mapped_column(String(320))
    issuance_status: Mapped[str] = mapped_column(String(16), nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(String(32))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        # Database uniqueness is what prevents a duplicate ticket for the same order position.
        UniqueConstraint("order_id", "ticket_number", name="uq_tickets_order_number"),
        Index("ix_tickets_user_issued", "user_id", "issued_at"),
        Index("ix_tickets_event_status", "event_id", "status"),
    )

    ticket_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("ticket_orders.order_id", name="fk_tickets_order"), nullable=False
    )
    ticket_number: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    category_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=VALID)
    qr_key_id: Mapped[str] = mapped_column(String(16), nullable=False)
    qr_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_in_by: Mapped[str | None] = mapped_column(String(128))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidation_reason: Mapped[str | None] = mapped_column(String(32))


class CheckIn(Base):
    __tablename__ = "check_ins"

    check_in_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.ticket_id", name="fk_check_ins_ticket"), nullable=False, unique=True
    )
    event_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    checked_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    checked_in_by: Mapped[str | None] = mapped_column(String(128))


class OrderRefund(Base):
    """A completed refund, kept so a late OrderConfirmed for the order is rejected."""

    __tablename__ = "order_refunds"

    order_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    refund_id: Mapped[str | None] = mapped_column(String(64))
    refund_amount: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(String(3))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutboxMessage(Base):
    """A message Ticket publishes, written in the same transaction as the change it reports.

    Today only TicketIssued: one row per issued order (and one more per republish), which
    notification-service turns into the buyer's ticket email. The worker publishes due rows to
    `ticket.events` and marks them PUBLISHED once the broker confirms. The payload is built
    when the row is published, from the order's tickets that are still valid."""

    __tablename__ = "outbox_messages"
    __table_args__ = (
        Index("ix_outbox_messages_due", "status", "next_attempt_at"),
        Index("ix_outbox_messages_order", "order_id", "id"),
    )

    # Insertion order: the latest row for an order is the one with the highest id.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    routing_key: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("ticket_orders.order_id", name="fk_outbox_messages_order"), nullable=False
    )
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class EmailDelivery(Base):
    """No longer written: ticket emails now come from notification-service (TicketIssued).

    The table stays so the previous image still runs against this schema during a deploy or a
    rollback (migrations are additive). Drop it in a later migration."""

    __tablename__ = "email_deliveries"
    __table_args__ = (Index("ix_email_deliveries_due", "status", "next_attempt_at"),)

    order_id: Mapped[str] = mapped_column(
        ForeignKey("ticket_orders.order_id", name="fk_email_deliveries_order"), primary_key=True
    )
    recipient: Mapped[str | None] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ProcessedMessage(Base):
    __tablename__ = "processed_messages"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
