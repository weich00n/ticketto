"""Builders for test data. Timestamps are whole milliseconds (DATETIME(3) precision)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import QueueConfig
from app.core.status import QueueStatus, active_pool_capacity
from app.queue import keys
from app.queue.config_repo import QueueConfigData

DEFAULT_RELEASE = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def event_id() -> str:
    return str(uuid.uuid4())


def user_id() -> str:
    return str(uuid.uuid4())


def queue_config(**overrides: Any) -> QueueConfig:
    """A queue_configs row with derived fields computed like EventPublished does."""
    release_at: datetime = overrides.pop("release_at", DEFAULT_RELEASE)
    total_capacity: int = overrides.pop("total_capacity", 304)
    values: dict[str, Any] = {
        "event_id": event_id(),
        "event_version": 1,
        "total_capacity": total_capacity,
        "active_pool_capacity": active_pool_capacity(total_capacity),
        "queue_opens_at": release_at - timedelta(minutes=15),
        "release_at": release_at,
        "status": QueueStatus.SCHEDULED,
        "closed_at": None,
        "sale_ends_at": None,
    }
    values.update(overrides)
    return QueueConfig(**values)


async def insert_config(sessions: async_sessionmaker[AsyncSession], **overrides: Any) -> str:
    row = queue_config(**overrides)
    async with sessions.begin() as session:
        session.add(row)
    return row.event_id


def config_data(**overrides: Any) -> QueueConfigData:
    """A QueueConfigData (no MySQL row), with derived fields computed as usual."""
    row = queue_config(**overrides)
    return QueueConfigData.from_row(row)


async def put_config(redis: Redis, cfg: QueueConfigData) -> None:
    """Write cfg straight into the Redis config cache (Redis-only tests)."""
    fields: dict[Any, Any] = dict(cfg.to_cache())  # redis-py's mapping type is invariant
    await redis.hset(keys.config(cfg.event_id), mapping=fields)
