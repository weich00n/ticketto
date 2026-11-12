"""Redis client factory and the server clock."""

from __future__ import annotations

from datetime import datetime

from redis.asyncio import Redis

from app.core.timefmt import from_ms


def create_redis(url: str) -> Redis:
    return Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=30,
    )


async def server_now(redis: Redis) -> datetime:
    """Current time from Redis TIME (ms precision), the only clock domain logic uses."""
    seconds, micros = await redis.time()
    return from_ms(int(seconds) * 1000 + int(micros) // 1000)
