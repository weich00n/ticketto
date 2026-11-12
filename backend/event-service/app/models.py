from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .status import EventStatus

# Microsecond precision on MySQL so outbox messages keep their order.
PreciseDateTime = DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


class Event(Base):
    __tablename__ = "events"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organiser_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    venue: Mapped[str] = mapped_column(String(200), nullable=False)
    # What kind of event it is, for browsing. Events made before this field have none.
    category: Mapped[str | None] = mapped_column(String(32))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ticket_release_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EventStatus.DRAFT, index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(String(500))
    event_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Counts image uploads: 0 means no image, and it goes in the image URL to refresh caches.
    image_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    ticket_categories: Mapped[list["TicketCategory"]] = relationship(
        back_populates="event", cascade="all, delete-orphan", order_by="TicketCategory.position"
    )


class EventImage(Base):
    __tablename__ = "event_images"

    event_id: Mapped[str] = mapped_column(
        ForeignKey("events.event_id", ondelete="CASCADE"), primary_key=True
    )
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Kept out of the events table so listing events never loads image bytes.
    data: Mapped[bytes] = mapped_column(
        LargeBinary().with_variant(mysql.MEDIUMBLOB(), "mysql"), nullable=False
    )


class TicketCategory(Base):
    __tablename__ = "ticket_categories"
    __table_args__ = (UniqueConstraint("event_id", "name", name="uq_ticket_categories_event_name"),)

    category_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(
        ForeignKey("events.event_id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    price_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[Event] = relationship(back_populates="ticket_categories")


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"

    message_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    message_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    routing_key: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(PreciseDateTime, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
