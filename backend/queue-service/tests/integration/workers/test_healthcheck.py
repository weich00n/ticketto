"""The worker healthcheck against real Redis heartbeats."""

from __future__ import annotations

import pytest
from redis.asyncio import Redis

from app.core.redis import server_now
from app.core.timefmt import to_ms
from workers.healthcheck import HEARTBEAT_KEYS, check

pytestmark = pytest.mark.integration


async def test_admitter_healthy_only_with_a_fresh_heartbeat(redis: Redis) -> None:
    assert await check(redis, "admitter") == ["queue:admitter:heartbeat: no heartbeat"]

    await redis.set("queue:admitter:heartbeat", to_ms(await server_now(redis)))
    assert await check(redis, "admitter") == []

    await redis.set("queue:admitter:heartbeat", to_ms(await server_now(redis)) - 60_000)
    (problem,) = await check(redis, "admitter")
    assert "last heartbeat" in problem


async def test_consumer_needs_every_queue_beating(redis: Redis) -> None:
    now = to_ms(await server_now(redis))
    *beating, silent = HEARTBEAT_KEYS["consumer"]
    for key in beating:
        await redis.set(key, now)

    assert await check(redis, "consumer") == [f"{silent}: no heartbeat"]

    await redis.set(silent, now)
    assert await check(redis, "consumer") == []
