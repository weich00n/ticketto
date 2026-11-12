"""RabbitMQ consumers (pika), following the team broker's contract (rabbitmq-service).

The broker owns the topology. rabbitmq-service creates every exchange, queue, binding and
dead-letter queue from its topology/messages.yaml, and Ticket's broker user may only read its
own queues, `ticket-service.<Message>`. So Ticket declares nothing: each queue is consumed by
name, in its own thread with its own blocking connection. Per delivery (manual ack):

  parse -> poison?           reject(requeue=False): straight to the queue's DLQ
        -> type not used?    ack and ignore
        -> handler ok        ack (only after the database transaction commits)
        -> handler raised    reject(requeue=True) after a short pause; the broker moves it to
                             the DLQ after DELIVERY_LIMIT redeliveries

basic_reject, never basic_nack: on the broker's RabbitMQ (4.3) a nack with requeue does not
count towards the delivery limit, so a message that always failed would loop forever.

A queue the broker doesn't have yet (or that Ticket's user can't read yet) is not fatal: the
consumer logs it and checks again every QUEUE_MISSING_RETRY_SECONDS, so the worker keeps
consuming its other queues until rabbitmq-service is deployed with the new queue.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import pika
import pika.exceptions
from pika.adapters.blocking_connection import BlockingChannel
from sqlalchemy.orm import Session, sessionmaker

from .correlation import bound_correlation_id
from .issuance import handle_message
from .messages import Message, PoisonMessage, parse_message
from .qr import QrSigner

logger = logging.getLogger(__name__)

SERVICE = "ticket-service"
# The broker's `consumer-queues` policy: redeliveries before a message is dead-lettered.
DELIVERY_LIMIT = 5
MAX_ATTEMPTS = DELIVERY_LIMIT + 1
PREFETCH = 10
MAX_BACKOFF_SECONDS = 30
QUEUE_MISSING_RETRY_SECONDS = 60

NOT_FOUND = 404
ACCESS_REFUSED = 403


@dataclass(frozen=True)
class ConsumerQueue:
    """One of Ticket's queues on the broker. The exchange and routing key are the broker's
    binding (topology/messages.yaml); Ticket only needs the queue name."""

    message_type: str
    exchange: str
    routing_key: str

    @property
    def name(self) -> str:
        return f"{SERVICE}.{self.message_type}"

    @property
    def dlq(self) -> str:
        return f"{self.name}.dlq"


ORDER_CONFIRMED = ConsumerQueue("OrderConfirmed", "checkout.events", "order.confirmed")
EVENT_CANCELLED = ConsumerQueue("EventCancelled", "event.events", "event.cancelled")
REFUND_COMPLETED = ConsumerQueue("RefundCompleted", "checkout.events", "refund.completed")
EVENT_PUBLISHED = ConsumerQueue("EventPublished", "event.events", "event.published")
CONSUMER_QUEUES = (ORDER_CONFIRMED, EVENT_CANCELLED, REFUND_COMPLETED, EVENT_PUBLISHED)


class QueueUnavailable(Exception):
    """The broker has no such queue yet, or Ticket's user may not read it yet."""

    def __init__(self, queue: str, reply_code: int, reply_text: str):
        super().__init__(f"{queue}: {reply_code} {reply_text}")
        self.queue = queue
        self.reply_code = reply_code
        self.reply_text = reply_text


class Disposition(StrEnum):
    ACK = "ack"
    REQUEUE = "requeue"  # transient failure: try again
    DEAD_LETTER = "dead_letter"  # poison: never retry


def dispose(
    body: bytes, run: Callable[[Message], object], *, attempt: int = 1, amqp_correlation_id=None
) -> Disposition:
    """Decide what to do with one delivery. `run` processes a parsed message."""
    # Without an AMQP correlation_id, issuance falls back to the envelope's, then message_id.
    with bound_correlation_id(amqp_correlation_id, generate=False):
        try:
            message = parse_message(body)
        except PoisonMessage as exc:
            logger.error("poison message dead-lettered", extra={"reason": str(exc)})
            return Disposition.DEAD_LETTER
        if message is None:
            return Disposition.ACK  # a message type Ticket does not use
        try:
            run(message)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception(
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


def settle(channel: BlockingChannel, delivery_tag: int, disposition: Disposition) -> None:
    """Acknowledge or reject one delivery. Always basic_reject, never basic_nack (see above)."""
    if disposition is Disposition.ACK:
        channel.basic_ack(delivery_tag)
    elif disposition is Disposition.DEAD_LETTER:
        channel.basic_reject(delivery_tag, requeue=False)
    else:
        channel.basic_reject(delivery_tag, requeue=True)


def _attempt(properties: pika.BasicProperties) -> int:
    """1 for the first delivery; quorum queues count redeliveries in a header."""
    headers: dict[str, Any] = properties.headers or {}
    return int(headers.get("x-delivery-count", 0)) + 1


def check_queue(channel: BlockingChannel, queue: str) -> None:
    """Passive declare: checks the queue exists without creating or changing anything (and
    needs no configure permission). Raises QueueUnavailable on 404 or 403."""
    try:
        channel.queue_declare(queue, passive=True)
    except pika.exceptions.ChannelClosedByBroker as exc:
        if exc.reply_code in (NOT_FOUND, ACCESS_REFUSED):
            raise QueueUnavailable(queue, exc.reply_code, exc.reply_text) from exc
        raise


class QueueConsumer:
    """Consumes one queue until `stop` is set. Call run() in its own thread."""

    def __init__(
        self,
        queue: ConsumerQueue,
        rabbitmq_url: str,
        handle: Callable[[Message], object],
        stop: threading.Event,
    ):
        self.queue = queue
        self._url = rabbitmq_url
        self._handle = handle
        self._stop = stop
        self.connected = threading.Event()

    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._consume()
                backoff = 1.0
            except QueueUnavailable as exc:
                self.connected.clear()
                logger.warning(
                    "queue not available on the broker yet; it is created by rabbitmq-service "
                    "from topology/messages.yaml",
                    extra={
                        "queue": exc.queue,
                        "reply_code": exc.reply_code,
                        "error": exc.reply_text,
                        "retry_in": QUEUE_MISSING_RETRY_SECONDS,
                    },
                )
                self._stop.wait(QUEUE_MISSING_RETRY_SECONDS)
            except (pika.exceptions.AMQPError, OSError) as exc:
                self.connected.clear()
                logger.warning(
                    "rabbitmq connection lost; reconnecting",
                    extra={"queue": self.queue.name, "error": repr(exc), "retry_in": backoff},
                )
                self._stop.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    def _consume(self) -> None:
        connection = pika.BlockingConnection(pika.URLParameters(self._url))
        try:
            channel = connection.channel()
            check_queue(channel, self.queue.name)
            channel.basic_qos(prefetch_count=PREFETCH)

            def on_message(ch: BlockingChannel, method, properties, body: bytes) -> None:
                attempt = _attempt(properties)
                disposition = dispose(
                    body,
                    self._handle,
                    attempt=attempt,
                    amqp_correlation_id=properties.correlation_id,
                )
                if disposition is Disposition.REQUEUE:
                    # Give a briefly unavailable database a moment before the redelivery.
                    self._stop.wait(min(0.5 * 2 ** (attempt - 1), 5))
                settle(ch, method.delivery_tag, disposition)

            # The broker cancels the consumer if the queue is deleted (e.g. while rabbitmq-service
            # recreates it). The connection stays open, so without this the thread would sit idle
            # and never consume the recreated queue.
            cancelled = threading.Event()
            channel.add_on_cancel_callback(lambda _frame: cancelled.set())
            channel.basic_consume(self.queue.name, on_message_callback=on_message, auto_ack=False)
            self.connected.set()
            logger.info("consuming", extra={"queue": self.queue.name})
            while not self._stop.is_set():
                connection.process_data_events(time_limit=1)
                if cancelled.is_set():
                    raise QueueUnavailable(
                        self.queue.name, NOT_FOUND, "consumer cancelled by the broker"
                    )
        finally:
            self.connected.clear()
            if connection.is_open:
                connection.close()


def start_consumers(
    rabbitmq_url: str,
    session_factory: "sessionmaker[Session]",
    signer: QrSigner,
    stop: threading.Event,
    crashed: threading.Event,
) -> list[threading.Thread]:
    def handle(message: Message) -> object:
        return handle_message(session_factory, signer, message)

    threads = []
    for queue in CONSUMER_QUEUES:
        consumer = QueueConsumer(queue, rabbitmq_url, handle, stop)
        thread = threading.Thread(
            target=_guarded(consumer, stop, crashed), name=f"consumer:{queue.name}", daemon=True
        )
        thread.start()
        threads.append(thread)
    return threads


def _guarded(consumer: QueueConsumer, stop: threading.Event, crashed: threading.Event):
    """If a consumer thread dies unexpectedly, stop the whole process so the orchestrator
    restarts it, rather than running with a queue unconsumed."""

    def target() -> None:
        try:
            consumer.run()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("consumer crashed; stopping", extra={"queue": consumer.queue.name})
            crashed.set()
            stop.set()

    return target
