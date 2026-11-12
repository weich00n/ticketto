from datetime import datetime

from sqlalchemy import CHAR, Boolean, DateTime, Enum, String, Text, true
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import functions

from app.db import Base

ROLE_BUYER = "buyer"
ROLE_ORGANISER = "organiser"


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    cognito_sub: Mapped[str] = mapped_column(CHAR(36), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        Enum(ROLE_BUYER, ROLE_ORGANISER, name="user_role"),
        nullable=False,
        default=ROLE_BUYER,
        server_default=ROLE_BUYER,
    )
    # New-event emails are on unless the user turns them off (opt-out).
    notify_new_events: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    # Comma-separated IDs of the events this user has bought tickets for, from OrderConfirmed.
    # Cancelled events stay listed, so notification-service can still email their buyers.
    purchased_event_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=functions.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=functions.now(), onupdate=functions.now()
    )


class CancelledEvent(Base):
    """An event cancelled by its organiser (from EventCancelled). A later OrderConfirmed for
    it is not added to the buyer's purchased events."""

    __tablename__ = "cancelled_events"

    event_id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    cancelled_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=functions.now()
    )
