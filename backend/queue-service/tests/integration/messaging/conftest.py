"""Fixtures for handler, topology and consumer tests.

- `handlers`: MessageHandlers on the real test MySQL + Redis (1 s / 2 s deadlines).
- `rabbit`: a channel on the test RabbitMQ with the topology declared and every
  Queue-owned queue purged, so tests start empty.
- `start_consumer`: runs a real QueueConsumer thread; stopped after the test.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pika
import pytest
from pika.adapters.blocking_connection import BlockingChannel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.messaging.consumers import Handler, QueueConsumer
from app.messaging.handlers import HandleResult, MessageHandlers
from app.messaging.schemas import parse_message
from app.messaging.topology import CONSUMER_QUEUES, ConsumerQueue, declare_topology
from app.queue.config_repo import ConfigRepository
from app.queue.repository import QueueRepository
from tests.conftest import MySQLInfo
from tests.fakes.messages import OutgoingMessage

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
def configs(sessions: Sessions, redis: Redis) -> ConfigRepository:
    return ConfigRepository(sessions, redis)


@pytest.fixture
def queues(redis: Redis) -> QueueRepository:
    return QueueRepository(redis, selection_window_seconds=1, admission_window_seconds=2)


@pytest.fixture
def handlers(
    sessions: Sessions, redis: Redis, configs: ConfigRepository, queues: QueueRepository
) -> MessageHandlers:
    return MessageHandlers(sessions, redis, configs, queues)


async def deliver(handlers: MessageHandlers, message: OutgoingMessage) -> HandleResult:
    """Run a message through the handlers exactly as the consumer would."""
    return await handlers.handle(parse_message(message.body))


async def eventually(
    check: Callable[[], Awaitable[bool]], within: float = 10.0, interval: float = 0.1
) -> None:
    """Poll `check` until it's true; fail after `within` seconds."""
    deadline = time.monotonic() + within
    while True:
        if await check():
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {within}s")
        await asyncio.sleep(interval)


# --- RabbitMQ ------------------------------------------------------------------------------


@pytest.fixture
def rabbit(rabbitmq_url: str) -> Iterator[BlockingChannel]:
    connection = pika.BlockingConnection(pika.URLParameters(rabbitmq_url))
    channel = connection.channel()
    declare_topology(channel)
    for queue in CONSUMER_QUEUES:
        channel.queue_purge(queue.name)
        channel.queue_purge(queue.dlq)
    yield channel
    if connection.is_open:
        connection.close()


@pytest.fixture
def consumer_settings(
    migrated_mysql: MySQLInfo, redis_url: str, rabbitmq_url: str, tmp_path: Path
) -> Settings:
    return Settings(
        mysql_url=migrated_mysql.async_url,
        redis_url=redis_url,
        rabbitmq_url=rabbitmq_url,
        jwt_private_key_path=tmp_path / "unused.pem",  # consumers never sign tokens
        jwt_kid="unused",  # production deadlines: nothing expires mid-test
    )


StartConsumer = Callable[..., QueueConsumer]


@pytest.fixture
def start_consumer(rabbit: BlockingChannel, consumer_settings: Settings) -> Iterator[StartConsumer]:
    running: list[tuple[threading.Event, threading.Thread]] = []

    def start(
        queue: ConsumerQueue,
        handler_override: Callable[[MessageHandlers], Handler] | None = None,
    ) -> QueueConsumer:
        stop = threading.Event()
        consumer = QueueConsumer(queue, consumer_settings, stop, handler_override=handler_override)
        thread = threading.Thread(target=consumer.run, name=f"test-consumer:{queue.name}")
        thread.start()
        running.append((stop, thread))
        assert consumer.connected.wait(10), f"consumer for {queue.name} didn't connect"
        return consumer

    yield start
    for stop, _thread in running:
        stop.set()
    for _, thread in running:
        thread.join(timeout=10)
        assert not thread.is_alive(), f"{thread.name} didn't stop"
