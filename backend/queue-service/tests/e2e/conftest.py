"""The whole service in one test process: the real API app (lifespan included),
the admitter loop and the three RabbitMQ consumer threads, all against the test
stack, with short deadlines. Other services are played by tests/fakes only.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from random import Random

import httpx
import pytest
from pika.adapters.blocking_connection import BlockingChannel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import Services
from app.core.config import Settings
from app.main import create_app
from app.messaging.consumers import QueueConsumer
from app.messaging.topology import CONSUMER_QUEUES
from app.queue.admitter import Admitter
from tests.conftest import MySQLInfo
from tests.fakes.checkout_service import FakeCheckoutService
from tests.fakes.event_service import FakeEventService
from tests.fakes.reservation_service import FakeReservationService
from tests.fixtures.keys import TestKey
from tests.integration.messaging.conftest import rabbit  # noqa: F401  (fixture)

SELECTION_SECONDS = 3
ADMISSION_SECONDS = 6
TICK_SECONDS = 0.2


@dataclass
class System:
    client: httpx.AsyncClient
    services: Services
    redis: Redis
    events: FakeEventService
    reservation: FakeReservationService
    checkout: FakeCheckoutService

    @staticmethod
    def headers(user_id: str) -> dict[str, str]:
        return {"X-User-Sub": user_id}


@pytest.fixture
async def system(
    migrated_mysql: MySQLInfo,
    redis_url: str,
    rabbitmq_url: str,
    redis: Redis,  # empties Redis
    sessions: async_sessionmaker[AsyncSession],  # empties MySQL
    rabbit: BlockingChannel,  # noqa: F811  (declares topology, purges queues)
    signing_key: TestKey,
    tmp_path: Path,
) -> AsyncIterator[System]:
    settings = Settings(
        mysql_url=migrated_mysql.async_url,
        redis_url=redis_url,
        rabbitmq_url=rabbitmq_url,
        jwt_private_key_path=signing_key.write_private(tmp_path),
        jwt_kid=signing_key.kid,
        admitter_tick_seconds=TICK_SECONDS,
        selection_window_seconds=SELECTION_SECONDS,
        admission_window_seconds=ADMISSION_SECONDS,
    )

    stop_consumers = threading.Event()
    consumers = [QueueConsumer(q, settings, stop_consumers) for q in CONSUMER_QUEUES]
    threads = [threading.Thread(target=c.run, name=f"e2e:{c.queue.name}") for c in consumers]
    for thread in threads:
        thread.start()

    app = create_app(settings, rng=Random(0))
    stop_admitter = asyncio.Event()
    try:
        for consumer in consumers:
            assert consumer.connected.wait(10), f"{consumer.queue.name} didn't connect"
        async with app.router.lifespan_context(app):
            services: Services = app.state.services
            admitter = Admitter(
                services.configs,
                services.queues,
                services.redis,
                tick_seconds=TICK_SECONDS,
                shuffle_rng=Random(0),
            )
            admitter_task = asyncio.create_task(admitter.run(stop_admitter))
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                try:
                    yield System(
                        client=client,
                        services=services,
                        redis=redis,
                        events=FakeEventService(rabbit),
                        reservation=FakeReservationService(rabbit),
                        checkout=FakeCheckoutService(rabbit),
                    )
                finally:
                    stop_admitter.set()
                    await asyncio.wait_for(admitter_task, timeout=5)
    finally:
        stop_consumers.set()
        for thread in threads:
            thread.join(timeout=10)
