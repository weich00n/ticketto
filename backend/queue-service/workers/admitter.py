"""Admitter entrypoint: python -m workers.admitter  (exactly one instance).

SIGTERM/SIGINT finish the tick in progress, then close connections and exit.
"""

from __future__ import annotations

import asyncio
import signal

from app.core.config import Settings, get_settings
from app.core.db import create_engine, create_session_factory
from app.core.logging import configure_logging
from app.core.redis import create_redis
from app.queue.admitter import Admitter
from app.queue.config_repo import ConfigRepository
from app.queue.repository import QueueRepository


async def run(settings: Settings, stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    redis = create_redis(settings.redis_url)
    engine = create_engine(settings.mysql_url)
    try:
        admitter = Admitter(
            ConfigRepository(create_session_factory(engine), redis),
            QueueRepository(
                redis,
                selection_window_seconds=settings.selection_window_seconds,
                admission_window_seconds=settings.admission_window_seconds,
            ),
            redis,
            tick_seconds=settings.admitter_tick_seconds,
        )
        await admitter.run(stop)
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        await redis.aclose()
        await engine.dispose()


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
