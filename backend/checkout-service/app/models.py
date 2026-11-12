"""Checkout's durable record.

Four tables carry the three split state machines (booking / payment / refund) plus the
idempotency, dedupe and outbox guards that make at-least-once delivery and retries safe
(see the wiki checkout-service-spec page). Money is integer minor units (SGD); one
database per service; every row belongs to exactly one hold.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# Microsecond precision on MySQL so outbox messages keep their order.
PreciseDateTime = DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


class Order(Base):
    """The booking state machine: one order per hold.

    PENDING_PAYMENT -> CONFIRMING -> CONFIRMED | EXPIRED | CANCELLED
    """

    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    hold_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    admission_id: Mapped[str | None] = mapped_column(String(36))
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    category_id: Mapped[str | None] = mapped_column(String(36))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    delivery_email: Mapped[str | None] = mapped_column(String(320))

    booking_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING_PAYMENT", index=True
    )
    # The hold's deadline, copied so an order can expire on its own without re-querying.
    # Never restarted by creating or retrying the order (Technical Contract section 7).
    payment_expires_at: Mapped[datetime] = mapped_column(PreciseDateTime, nullable=False)
    hold_expires_at: Mapped[datetime] = mapped_column(PreciseDateTime, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(PreciseDateTime)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Eager (selectin) by default: the routes serialise a detached Order, so the
    # payment and refund rows must come back with it - a lazy load after the session
    # closes would raise DetachedInstanceError on every order read.
    payment: Mapped["Payment | None"] = relationship(
        back_populates="order", uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )
    refunds: Mapped[list["Refund"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Order {self.order_id} {self.booking_status}>"


class Payment(Base):
    """The payment state machine, one row per order for the MVP.

    PENDING -> SUCCEEDED | FAILED | CANCELLED
    The payment is independent of the booking: an order can be CANCELLED while its
    payment is SUCCEEDED (a refund is then pending) - the two machines stay separate
    (Technical Contract section 9).
    """

    __tablename__ = "payments"

    payment_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    # Stripe's payment_intent id; unique so a duplicate intent can never map to two orders.
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING", index=True)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    succeeded_at: Mapped[datetime | None] = mapped_column(PreciseDateTime)
    failed_at: Mapped[datetime | None] = mapped_column(PreciseDateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    order: Mapped["Order"] = relationship(back_populates="payment")


class Refund(Base):
    """The refund workflow, 0..1 rows per order.

    REQUESTED -> PENDING -> SUCCEEDED | FAILED | REQUIRES_ACTION
    A refund obligation is recorded (REQUESTED) before Stripe is asked, and retries use
    the same idempotency_key so one logical refund never produces two Stripe refunds.
    """

    __tablename__ = "refunds"
    __table_args__ = (
        # An order has one payment, so at most one (full) refund, whatever caused it.
        UniqueConstraint("order_id", name="uq_refunds_order_id"),
    )

    refund_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False, index=True
    )
    stripe_refund_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    # Stable per logical refund; reused across retries so Stripe de-duplicates.
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="REQUESTED", index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    order: Mapped["Order"] = relationship(back_populates="refunds")


class IdempotencyKey(Base):
    """Client-side replay guard for POST /orders (Technical Contract section 10).

    The key exists before the order does: we insert key + order in one commit, and a
    retry with the same key returns the original order. Reusing a key with a different
    payload is a conflict.
    """

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Stable fingerprint of the request body so we can detect a different-payload reuse.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ProcessedMessage(Base):
    """Consumer-side dedupe: one row per consumed AMQP message_id.

    Inserted in the same transaction as the state change the message caused, so a redelivered
    message (at-least-once) is a no-op. The unique message_id is what makes replay safe.
    """

    __tablename__ = "processed_messages"

    message_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ProcessedStripeEvent(Base):
    """Provider-side replay guard: one row per Stripe event id.

    Stripe delivers webhooks at-least-once; a duplicate event id is a no-op, so a replayed
    payment_intent.succeeded can never re-fire the confirm/refund race.
    """

    __tablename__ = "processed_stripe_events"

    stripe_event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OutboxMessage(Base):
    """The transactional outbox: a message exists iff its state change committed.

    The stored payload is the full broker envelope, so the relay just publishes it
    unchanged to checkout.events with the stored routing key.
    """

    __tablename__ = "outbox_messages"

    # Autoincrement id: the relay orders by it so messages publish in commit order.
    # BIGINT on MySQL, but plain INTEGER on SQLite - SQLite only gives the rowid alias
    # (and hence autoincrement) to a column declared exactly INTEGER PRIMARY KEY.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    message_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    # The envelope's message_type (PascalCase), also sent as the AMQP `type` property.
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # The dotted routing key from the broker's messages.yaml, e.g. order.confirmed.
    routing_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # Aggregate the message is about (the order, or the refund for refund messages); with
    # the message type it determines message_id, so one fact is one message.
    aggregate_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(PreciseDateTime, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
