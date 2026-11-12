from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

# Notification kinds: one per consumed message
KIND_EVENT_PUBLISHED = "EVENT_PUBLISHED"
KIND_EVENT_CANCELLED = "EVENT_CANCELLED"
KIND_ORDER_REFUNDED = "ORDER_REFUNDED"
KIND_REFUND_FAILED = "REFUND_FAILED"
KIND_TICKET_ISSUED = "TICKET_ISSUED"
KINDS = (
    KIND_EVENT_PUBLISHED,
    KIND_EVENT_CANCELLED,
    KIND_ORDER_REFUNDED,
    KIND_REFUND_FAILED,
    KIND_TICKET_ISSUED,
)

# Notification status
STATUS_PENDING = "PENDING"
STATUS_SENT = "SENT"
STATUS_FAILED = "FAILED"
STATUS_CANCELLED = "CANCELLED"

# All times are naive UTC (the MySQL session runs in UTC).


class Event(Base):
    """What this service knows about an event. Its row is also the per-event lock:
    EventPublished and EventCancelled for one event lock it, so they apply one at a time and
    a cancellation always wins over a publish, whichever arrives first."""

    __tablename__ = "events"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str | None] = mapped_column(String(160))
    starts_at: Mapped[datetime | None] = mapped_column(DateTime)
    announced_at: Mapped[datetime | None] = mapped_column(DateTime)  # new-event emails queued
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Notification(Base):
    """One email to one person, sent in the background with retries."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_due", "status", "next_attempt_at"),
        Index("ix_notifications_event", "event_id", "kind", "status"),
        Index("ix_notifications_inbox", "cognito_sub", "created_at"),
    )

    # BIGINT in MySQL; SQLite only autoincrements INTEGER primary keys.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # Unique: the same person never gets the same email twice, even if the message is sent
    # again under a new message_id. <kind>:<event_id>:<user_id>, or <kind>:<refund> for refunds.
    # Ticket emails are <kind>:<message_id>: a new message_id is a deliberate resend.
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    source_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[str | None] = mapped_column(String(36))
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # The recipient's Cognito sub (lower case): how the notifications API finds a signed-in
    # user's rows. NULL on rows queued before it was stored.
    cognito_sub: Mapped[str | None] = mapped_column(String(64))
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    # What the template needs (title, times, reason, amount...), captured when queued. A ticket
    # email's QR codes are here only until it's sent, fails for good or is cancelled.
    context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    # When the user saw it in the app (the bell or the notifications page); NULL = unread.
    read_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class ProcessedMessage(Base):
    __tablename__ = "processed_messages"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
