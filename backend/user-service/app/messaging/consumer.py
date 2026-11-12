"""pika consumers: one thread per user-service queue, each with its own connection.

Per delivery (manual ack, prefetch 10), following rabbitmq-service docs/MESSAGING.md:
  poison                     -> basic_reject(requeue=False): the queue's DLQ takes it at once
  handler raised (transient) -> basic_reject(requeue=True) after a short pause; the broker
                                dead-letters it after 5 redeliveries
  ok                         -> basic_ack, only after the DB transaction committed

Never basic_nack: on RabbitMQ 4.3 a nack with requeue does not count towards the delivery
limit, so a message that always fails would loop forever. The broker owns the topology:
queues are consumed by name and never declared.
"""

import logging
import threading
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any

import pika
import pika.exceptions

from app.context import correlation_id_var
from app.messaging.envelope import Message, PoisonMessage, parse

logger = logging.getLogger(__name__)

QUEUES = ("user-service.OrderConfirmed", "user-service.EventCancelled")
PREFETCH = 10
MAX_BACKOFF_SECONDS = 30


class Disposition(StrEnum):
    ACK = "ack"
    REQUEUE = "requeue"
    DEAD_LETTER = "dead_letter"


def _attempt(properties: Any) -> int:
    headers: dict = getattr(properties, "headers", None) or {}
    return int(headers.get("x-delivery-count", 0)) + 1


def dispose(
    body: bytes,
    run: Callable[[Message], object],
    *,
    correlation_id: str | None = None,
    attempt: int = 1,
) -> Disposition:
    try:
        message = parse(body, correlation_id)
    except PoisonMessage as exc:
        logger.error("poison message dead-lettered", extra={"reason": str(exc)})
        return Disposition.DEAD_LETTER
    if message.payload is None:
        return Disposition.ACK  # a message type user-service doesn't use
    token = correlation_id_var.set(message.correlation_id or message.message_id)
    try:
        run(message)
    except Exception:  # pylint: disable=broad-exception-caught  # retried via requeue
        logger.exception(
            "message handling failed; will be retried",
            extra={
                "message_id": message.message_id,
                "message_type": message.message_type,
                "attempt": attempt,
            },
        )
        return Disposition.REQUEUE
    finally:
        correlation_id_var.reset(token)
    return Disposition.ACK


def make_on_message(
    run: Callable[[Message], object], pause: Callable[[float], object] = time.sleep
) -> Callable:
    """The pika on_message callback for one queue."""

    def on_message(ch, method, properties, body: bytes) -> None:
        attempt = _attempt(properties)
        disposition = dispose(
            body, run, correlation_id=getattr(properties, "correlation_id", None), attempt=attempt
        )
        if disposition is Disposition.ACK:
            ch.basic_ack(method.delivery_tag)
        elif disposition is Disposition.DEAD_LETTER:
            ch.basic_reject(method.delivery_tag, requeue=False)
        else:
            # Give a briefly unavailable database a moment before the redelivery.
            pause(min(0.5 * 2 ** (attempt - 1), 5))
            ch.basic_reject(method.delivery_tag, requeue=True)

    return on_message


class QueueConsumer:
    """Consumes one queue until `stop` is set. Call run() in its own thread."""

    def __init__(
        self,
        queue: str,
        rabbitmq_url: str,
        run: Callable[[Message], object],
        stop: threading.Event,
    ) -> None:
        self.queue = queue
        self._url = rabbitmq_url
        self._run = run
        self._stop = stop

    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._consume()
                backoff = 1.0
            except (pika.exceptions.AMQPError, OSError) as exc:
                logger.warning(
                    "rabbitmq connection lost; reconnecting",
                    extra={"queue": self.queue, "error": repr(exc), "retry_in": backoff},
                )
                self._stop.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    def _consume(self) -> None:
        connection = pika.BlockingConnection(pika.URLParameters(self._url))
        try:
            channel = connection.channel()
            channel.basic_qos(prefetch_count=PREFETCH)
            channel.basic_consume(
                self.queue, on_message_callback=make_on_message(self._run, self._stop.wait)
            )
            logger.info("consuming", extra={"queue": self.queue})
            while not self._stop.is_set():
                connection.process_data_events(time_limit=1)
        finally:
            if connection.is_open:
                connection.close()


def start_consumers(
    rabbitmq_url: str,
    run: Callable[[Message], object],
    stop: threading.Event,
    crashed: threading.Event,
) -> list[threading.Thread]:
    threads = []
    for queue in QUEUES:
        consumer = QueueConsumer(queue, rabbitmq_url, run, stop)
        thread = threading.Thread(
            target=_guarded(consumer, stop, crashed), name=f"consumer:{queue}", daemon=True
        )
        thread.start()
        threads.append(thread)
    return threads


def _guarded(consumer: QueueConsumer, stop: threading.Event, crashed: threading.Event):
    """A consumer thread that dies stops the whole process, so ECS restarts it rather
    than running with a queue unconsumed."""

    def target() -> None:
        try:
            consumer.run()
        except Exception:  # pylint: disable=broad-exception-caught  # any crash is fatal
            logger.exception("consumer crashed; stopping", extra={"queue": consumer.queue})
            crashed.set()
            stop.set()

    return target
