from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis

from app.core.redis import server_now

pytestmark = pytest.mark.integration


async def test_server_now_is_aware_utc_with_ms_precision(redis: Redis) -> None:
    now = await server_now(redis)
    assert now.tzinfo is UTC
    assert now.microsecond % 1000 == 0
    # Container and host share a clock; allow generous skew.
    assert abs(now - datetime.now(UTC)) < timedelta(seconds=5)


async def test_server_now_is_monotonic_enough(redis: Redis) -> None:
    first = await server_now(redis)
    second = await server_now(redis)
    assert second >= first


async def test_client_decodes_responses(redis: Redis) -> None:
    await redis.set("k", "v")
    assert await redis.get("k") == "v"
