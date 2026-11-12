"""pika consumers: one thread per consumer queue, each with its own blocking
connection, asyncio loop, Redis client and MySQL engine.

Per delivery (manual ack, prefetch 50):
  parse -> poison?  reject(requeue=False): dead-lettered at once
        -> handler  ok: ack (only after MySQL/Redis changes are done)
                    raised: held unacked for retry_delay(attempt), then
                    reject(requeue=True); on attempt MAX_ATTEMPTS, reject(requeue=False)

Always reject, never nack: on RabbitMQ 4.3 nack(requeue=True) doesn't advance
x-delivery-count, so a message that always fails would loop forever.

Connection loss is retried with backoff. Setting `stop` ends each consumer
within about a second, after the message in progress.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import pika
import pika.exceptions
from pika.adapters.blocking_connection import BlockingChannel
from pika.spec import Basic, BasicProperties
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.db import create_engine, create_session_factory
from app.core.redis import create_redis, server_now
from app.core.timefmt import to_ms
from app.messaging.handlers import MessageHandlers
from app.messaging.schemas import Message, PoisonMessage, parse_message
from app.messaging.topology import (
    CONSUMER_QUEUES,
    MAX_ATTEMPTS,
    ConsumerQueue,
    check_queues,
    declare_topology,
)
from app.queue import keys
from app.queue.config_repo import ConfigRepository
from app.queue.repository import QueueRepository

log = logging.getLogger(__name__)

PREFETCH = 50
HEARTBEAT_EVERY_SECONDS = 5
HEARTBEAT_TTL_MS = 30_000
MAX_BACKOFF_SECONDS = 30
RETRY_BASE_DELAY_SECONDS = 1.0  # then doubles: 1, 2, 4, 8 s, about 15 s over all attempts

Handler = Callable[[Message], Awaitable[object]]


class Disposition(StrEnum):
    ACK = "ack"
    REQUEUE = "requeue"  # transient failure: try again
    DEAD_LETTER = "dead_letter"  # poison, or failed MAX_ATTEMPTS times: never retry


def dispose(body: bytes, run: Callable[[Message], object], attempt: int = 1) -> Disposition:
    """Decide what to do with one delivery. `run` processes a parsed message.

    The attempt cap is enforced here, not only by the queue's x-delivery-limit: the
    shared broker's queues have no limit (-1), so without it a message that always
    fails would be requeued forever instead of reaching its DLQ.
    """
    try:
        message = parse_message(body)
    except PoisonMessage as exc:
        log.error("poison message dead-lettered", extra={"reason": str(exc)})
        return Disposition.DEAD_LETTER
    try:
        run(message)
    except Exception:
        if attempt >= MAX_ATTEMPTS:
            log.exception(
                "message handling failed; dead-lettered after max attempts",
                extra={
                    "message_id": message.message_id,
                    "message_type": message.message_type,
                    "attempt": attempt,
                    "max_attempts": MAX_ATTEMPTS,
                },
            )
            return Disposition.DEAD_LETTER
        log.exception(
            "message handling failed; will be retried",
            extra={
                "message_id": message.message_id,
                "message_type": message.message_type,
                "attempt": attempt,
                "max_attempts": MAX_ATTEMPTS,
            },
        )
        return Disposition.REQUEUE
    return Disposition.ACK


def retry_delay(attempt: int) -> float:
    """Seconds to wait before redelivering a message that just failed `attempt`."""
    return float(RETRY_BASE_DELAY_SECONDS * 2 ** (attempt - 1))


def heartbeat_key(queue: ConsumerQueue) -> str:
    return keys.consumer_heartbeat(queue.name)


@dataclass
class _Resources:
    redis: Redis
    engine: AsyncEngine
    handle: Handler


class QueueConsumer:
    """Consumes one queue until `stop` is set. Call run() in its own thread."""

    def __init__(
        self,
        queue: ConsumerQueue,
        settings: Settings,
        stop: threading.Event,
        *,
        handler_override: Callable[[MessageHandlers], Handler] | None = None,
    ):
        self.queue = queue
        self._settings = settings
        self._stop = stop
        self._handler_override = handler_override
        self.connected = threading.Event()  # set while consuming (tests wait on it)

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        resources = loop.run_until_complete(self._open())
        try:
            backoff = 1.0
            while not self._stop.is_set():
                try:
                    self._consume(loop, resources)
                    backoff = 1.0
                except (pika.exceptions.AMQPError, OSError) as exc:
                    self.connected.clear()
                    log.warning(
                        "rabbitmq connection lost; reconnecting",
                        extra={"queue": self.queue.name, "error": repr(exc), "retry_in": backoff},
                    )
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        finally:
            self.connected.clear()
            loop.run_until_complete(self._close(resources))
            loop.close()

    async def _open(self) -> _Resources:
        redis = create_redis(self._settings.redis_url)
        engine = create_engine(self._settings.mysql_url)
        configs = ConfigRepository(create_session_factory(engine), redis)
        queues = QueueRepository(
            redis,
            selection_window_seconds=self._settings.selection_window_seconds,
            admission_window_seconds=self._settings.admission_window_seconds,
        )
        handlers = MessageHandlers(create_session_factory(engine), redis, configs, queues)
        handle = self._handler_override(handlers) if self._handler_override else handlers.handle
        return _Resources(redis, engine, handle)

    @staticmethod
    async def _close(resources: _Resources) -> None:
        await resources.redis.aclose()
        await resources.engine.dispose()

    def _consume(self, loop: asyncio.AbstractEventLoop, resources: _Resources) -> None:
        connection = pika.BlockingConnection(pika.URLParameters(self._settings.rabbitmq_url))
        try:
            channel = connection.channel()
            channel.basic_qos(prefetch_count=PREFETCH)

            def on_message(
                ch: BlockingChannel,
                method: Basic.Deliver,
                properties: BasicProperties,
                body: bytes,
            ) -> None:
                attempt = _attempt(properties)
                disposition = dispose(
                    body, lambda m: loop.run_until_complete(resources.handle(m)), attempt
                )
                if disposition is Disposition.ACK:
                    ch.basic_ack(method.delivery_tag)
                elif disposition is Disposition.DEAD_LETTER:
                    ch.basic_reject(method.delivery_tag, requeue=False)
                else:
                    # A timer, not a sleep: sleeping here would stall this loop, and with it
                    # the other deliveries and the heartbeat the healthcheck relies on.
                    connection.call_later(
                        retry_delay(attempt), lambda: _requeue(ch, method.delivery_tag)
                    )

            channel.basic_consume(self.queue.name, on_message_callback=on_message)
            self.connected.set()
            log.info("consuming", extra={"queue": self.queue.name})
            last_beat = 0.0
            while not self._stop.is_set():
                connection.process_data_events(time_limit=1)
                if time.monotonic() - last_beat >= HEARTBEAT_EVERY_SECONDS:
                    loop.run_until_complete(self._heartbeat(resources.redis))
                    last_beat = time.monotonic()
        finally:
            self.connected.clear()
            if connection.is_open:
                connection.close()

    async def _heartbeat(self, redis: Redis) -> None:
        try:
            now = await server_now(redis)
            await redis.set(heartbeat_key(self.queue), to_ms(now), px=HEARTBEAT_TTL_MS)
        except Exception:
            log.warning("consumer heartbeat failed", extra={"queue": self.queue.name})


def _requeue(channel: BlockingChannel, delivery_tag: int) -> None:
    # If the channel has gone, the broker has already requeued the message.
    if channel.is_open:
        channel.basic_reject(delivery_tag, requeue=True)


def _attempt(properties: BasicProperties) -> int:
    """1 for the first delivery; quorum queues count redeliveries in a header."""
    headers: dict[str, Any] = properties.headers or {}
    return int(headers.get("x-delivery-count", 0)) + 1


def declare_with_retry(settings: Settings, stop: threading.Event) -> bool:
    """Declare the topology (or, on a shared broker, check our queues exist),
    retrying until it succeeds or `stop` is set."""
    setup = declare_topology if settings.rabbitmq_declare_topology else check_queues
    backoff = 1.0
    while not stop.is_set():
        try:
            connection = pika.BlockingConnection(pika.URLParameters(settings.rabbitmq_url))
            try:
                setup(connection.channel())
            finally:
                if connection.is_open:
                    connection.close()
            return True
        except pika.exceptions.ChannelClosedByBroker as exc:
            # e.g. 404: one of our queues doesn't exist yet on the shared broker.
            log.error(
                "rabbitmq queue missing or not accessible",
                extra={"error": repr(exc), "retry_in": backoff},
            )
            stop.wait(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        except (pika.exceptions.AMQPError, OSError) as exc:
            log.warning("rabbitmq not ready", extra={"error": repr(exc), "retry_in": backoff})
            stop.wait(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
    return False


def start_consumers(
    settings: Settings, stop: threading.Event, crashed: threading.Event
) -> list[threading.Thread]:
    threads = []
    for queue in CONSUMER_QUEUES:
        consumer = QueueConsumer(queue, settings, stop)
        thread = threading.Thread(
            target=_guarded(consumer, stop, crashed), name=f"consumer:{queue.name}", daemon=True
        )
        thread.start()
        threads.append(thread)
    return threads


def _guarded(
    consumer: QueueConsumer, stop: threading.Event, crashed: threading.Event
) -> Callable[[], None]:
    """If a consumer thread dies unexpectedly, stop the whole process so the
    orchestrator restarts it, rather than running with a queue unconsumed."""

    def target() -> None:
        try:
            consumer.run()
        except Exception:
            log.exception("consumer crashed; stopping", extra={"queue": consumer.queue.name})
            crashed.set()
            stop.set()

    return target
