"""MySQL access: async engine/session factory and ORM models.

Timestamps are stored as naive UTC DATETIME(3) and exposed as aware UTC datetimes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import CHAR, Enum, Index, Integer, text
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.core.status import QueueStatus
from app.core.timefmt import ensure_utc


class UTCDateTime(TypeDecorator[datetime]):  # pylint: disable=abstract-method
    """DATETIME(3) holding UTC; rejects naive datetimes on write."""

    impl = DATETIME(fsp=3)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return ensure_utc(value).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


queue_status_enum = Enum(
    QueueStatus,
    name="status",
    values_callable=lambda statuses: [s.value for s in statuses],
    validate_strings=True,
)


class QueueConfig(Base):
    __tablename__ = "queue_configs"
    __table_args__ = (
        Index("idx_status_release", "status", "release_at"),
        Index("idx_status_opens", "status", "queue_opens_at"),
    )

    event_id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    event_version: Mapped[int] = mapped_column(Integer)
    total_capacity: Mapped[int] = mapped_column(Integer)
    active_pool_capacity: Mapped[int] = mapped_column(Integer)
    queue_opens_at: Mapped[datetime] = mapped_column(UTCDateTime)
    release_at: Mapped[datetime] = mapped_column(UTCDateTime)
    sale_ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[QueueStatus] = mapped_column(queue_status_enum)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        server_default=text("CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)"),
    )


def create_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True, pool_recycle=1800)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
