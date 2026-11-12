"""Fixtures for the Redis queue tests: repositories and events in a given state.

Times are relative to Redis TIME, never the test machine's clock.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from random import Random

import pytest
from redis.asyncio import Redis

from app.core.redis import server_now
from app.core.status import QueueStatus
from app.queue import keys
from app.queue.config_repo import QueueConfigData
from app.queue.repository import QueueRepository
from tests.fixtures.factories import config_data, put_config

# Short windows for sweep tests: selection 1 s, admission 2 s.
SHORT_SELECTION_S, SHORT_ADMISSION_S = 1, 2
SHORT_EXPIRY_WAIT_S = SHORT_ADMISSION_S + 0.2

MakeEvent = Callable[..., Awaitable[QueueConfigData]]


@pytest.fixture
def repo(redis: Redis) -> QueueRepository:
    """Production deadlines (5 / 11 min): nothing expires during a test."""
    return QueueRepository(redis)


@pytest.fixture
def short_repo(redis: Redis) -> QueueRepository:
    return QueueRepository(
        redis,
        selection_window_seconds=SHORT_SELECTION_S,
        admission_window_seconds=SHORT_ADMISSION_S,
    )


@pytest.fixture
def open_event(redis: Redis) -> MakeEvent:
    """An OPEN event (release 10 min away) whose config is in the cache."""

    async def make(capacity: int = 2) -> QueueConfigData:
        now = await server_now(redis)
        cfg = config_data(
            status=QueueStatus.OPEN,
            total_capacity=capacity * 5,
            release_at=now + timedelta(minutes=10),
        )
        await put_config(redis, cfg)
        return cfg

    return make


@pytest.fixture
def admitting_event(redis: Redis, repo: QueueRepository) -> MakeEvent:
    """An ADMITTING event (already shuffled), with `users` joined in FIFO order."""

    async def make(capacity: int = 2, users: Sequence[str] = ()) -> QueueConfigData:
        now = await server_now(redis)
        cfg = config_data(
            status=QueueStatus.ADMITTING,
            total_capacity=capacity * 5,
            release_at=now - timedelta(seconds=1),
        )
        await put_config(redis, cfg)
        assert (await repo.shuffle(cfg, Random(0))).performed
        for user in users:
            await repo.join(cfg, user)
        return cfg

    return make


async def set_cached_status(redis: Redis, cfg: QueueConfigData, status: QueueStatus) -> None:
    await redis.hset(keys.config(cfg.event_id), "status", status.value)


async def waiting_order(redis: Redis, cfg: QueueConfigData) -> list[str]:
    return [str(u) for u in await redis.zrange(keys.waiting(cfg.event_id), 0, -1)]
