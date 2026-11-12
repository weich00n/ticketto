"""The worker entrypoint stops cleanly on SIGTERM, even while every tick fails.

Points at closed ports, so it needs no test services.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import pytest

from app.core.config import Settings
from workers.admitter import run

pytestmark = pytest.mark.integration


async def test_sigterm_stops_the_admitter(tmp_path: Path) -> None:
    settings = Settings(
        mysql_url="mysql+asyncmy://queue:queue@127.0.0.1:1/queue_service",
        redis_url="redis://127.0.0.1:1/0",
        rabbitmq_url="amqp://unused",
        jwt_private_key_path=tmp_path / "unused.pem",  # the admitter never signs tokens
        jwt_kid="unused",
        admitter_tick_seconds=0.05,
    )
    task = asyncio.create_task(run(settings))
    await asyncio.sleep(0.3)  # several failing ticks; signal handlers are installed

    os.kill(os.getpid(), signal.SIGTERM)

    await asyncio.wait_for(task, timeout=3)
    assert task.done() and task.exception() is None
