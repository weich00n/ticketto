"""Container healthcheck for the workers, based on their Redis heartbeats.

    python -m workers.healthcheck admitter    # exit 0 healthy, 1 unhealthy
    python -m workers.healthcheck consumer    # every queue consumer must beat

The admitter writes its heartbeat every tick (~1 s, TTL 10 s); each consumer
thread every 5 s (TTL 30 s). A missing or stale heartbeat means the worker is
stuck, dead or can't reach Redis, so restart it.

ECS starts a new Python process for every check, on a fraction of a vCPU, so keep
the imports light: Redis, the settings and key names only (no SQLAlchemy, pika or
handlers). tests/unit/test_healthcheck.py enforces this.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.redis import create_redis, server_now
from app.core.timefmt import to_ms
from app.messaging.topology import CONSUMER_QUEUES
from app.queue import keys

HEARTBEAT_KEYS = {
    "admitter": [keys.ADMITTER_HEARTBEAT],
    "consumer": [keys.consumer_heartbeat(queue.name) for queue in CONSUMER_QUEUES],
}
MAX_AGE_MS = {"admitter": 10_000, "consumer": 30_000}


def evaluate(beats: dict[str, str | None], now_ms: int, max_age_ms: int) -> list[str]:
    """Problems with the given heartbeats (key -> ms value or None); empty = healthy."""
    problems = []
    for key, value in beats.items():
        if value is None:
            problems.append(f"{key}: no heartbeat")
            continue
        try:
            age = now_ms - int(value)
        except ValueError:
            problems.append(f"{key}: unreadable heartbeat {value!r}")
            continue
        if age > max_age_ms:
            problems.append(f"{key}: last heartbeat {age / 1000:.1f}s ago")
    return problems


async def check(redis: Redis, component: str) -> list[str]:
    names = HEARTBEAT_KEYS[component]
    values = await redis.mget(names)
    now_ms = to_ms(await server_now(redis))
    beats = {name: (None if v is None else str(v)) for name, v in zip(names, values, strict=True)}
    return evaluate(beats, now_ms, MAX_AGE_MS[component])


async def _run(component: str) -> list[str]:
    redis = create_redis(get_settings().redis_url)
    try:
        return await check(redis, component)
    except Exception as exc:
        return [f"redis unreachable: {exc!r}"]
    finally:
        await redis.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=sorted(HEARTBEAT_KEYS))
    args = parser.parse_args(argv)
    problems = asyncio.run(_run(args.component))
    if problems:
        print(f"{args.component} UNHEALTHY: " + "; ".join(problems), file=sys.stderr)
        return 1
    print(f"{args.component} healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
