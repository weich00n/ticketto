"""pika consumers: one thread per checkout queue, each with its own blocking
connection and session factory.

Per delivery (manual ack, prefetch 50):
  parse the broker envelope (correlation id from the AMQP property), check the message
  type belongs on this queue, then run the handler (which validates the payload)
     poison (bad envelope, wrong type, unusable payload)
              -> basic_reject(requeue=False): the queue's DLQ takes it at once
     handler raised (transient) -> basic_reject(requeue=True): RabbitMQ redelivers, and
        the broker's delivery_limit dead-letters it after 5 redeliveries
     ok -> basic_ack: only after the DB transaction committed

Never basic_nack: on RabbitMQ 4.3 a nack with requeue does not count towards the
delivery limit, so a message that always fails would loop forever (rabbitmq-service
docs/MESSAGING.md).

Connection loss is retried with backoff. A `stop` event ends each consumer within about
a second, after the message in progress.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import pika
import pika.exceptions
from pika.adapters.blocking_connection import BlockingChannel
from pika.spec import Basic, BasicProperties

from ..config import Settings
from ..correlation import correlation_id_var
from .envelope import Envelope, PoisonMessage, parse
from .handlers import MessageHandlers
from .payloads import QUEUE_MESSAGE_TYPES

log = logging.getLogger(__name__)

PREFETCH = 50
MAX_BACKOFF_SECONDS = 30

# Which handler consumes which queue (broker convention: queue name = "<service>.<Event>").
QUEUE_TO_HANDLER: dict[str, Callable[[MessageHandlers, Envelope], None]] = {
    "checkout-service.EventCancelled": lambda h, e: h.event_cancelled(e),
    "checkout-service.ReservationExpired": lambda h, e: h.reservation_expired(e),
    "checkout-service.RefundRequired": lambda h, e: h.refund_required(e),
}


def _attempt(properties: BasicProperties) -> int:
    headers: dict = properties.headers or {}
    return int(headers.get("x-delivery-count", 0)) + 1


def _check_type(envelope: Envelope, properties: BasicProperties, expected: str | None) -> None:
    """A queue carries exactly one message type, and the AMQP `type` property (when the
    publisher sets it, as MESSAGING.md requires) must agree with the envelope."""
    if expected is not None and envelope.message_type != expected:
        raise PoisonMessage(f"{envelope.message_type} arrived on the {expected} queue")
    amqp_type = getattr(properties, "type", None)
    if amqp_type and amqp_type != envelope.message_type:
        raise PoisonMessage(
            f"AMQP type {amqp_type!r} disagrees with message_type {envelope.message_type!r}"
        )


def _dispose(
    body: bytes,
    properties: BasicProperties,
    run: Callable[[Envelope], object],
    expected_type: str | None = None,
) -> tuple[bool, bool]:
    """Returns (ack, requeue)."""
    try:
        envelope = parse(body, properties.correlation_id)
        _check_type(envelope, properties, expected_type)
    except PoisonMessage as exc:
        log.error("poison message dead-lettered", extra={"reason": str(exc)})
        return False, False
    # Messages this one causes carry its correlation id on (Technical Contract section 2).
    token = correlation_id_var.set(envelope.correlation_id or envelope.message_id)
    try:
        run(envelope)
    except PoisonMessage as exc:
        # The payload can never be processed (missing field, records disagree): the
        # handler changed nothing, so dead-letter it now rather than after 5 retries.
        log.error(
            "poison payload dead-lettered",
            extra={"message_type": envelope.message_type, "reason": str(exc)},
        )
        return False, False
    except Exception:  # pylint: disable=broad-exception-caught  # handled below by (re)queue
        log.exception(
            "message handling failed; will be retried",
            extra={"message_type": envelope.message_type, "attempt": _attempt(properties)},
        )
        return False, True
    finally:
        correlation_id_var.reset(token)
    return True, False


class QueueConsumer:
    """Consumes one queue until `stop` is set. Call run() in its own thread."""

    def __init__(
        self, queue: str, settings: Settings, handlers: MessageHandlers, stop: threading.Event
    ) -> None:
        self.queue = queue
        self._settings = settings
        self._handlers = handlers
        self._stop = stop
        self.connected = threading.Event()

    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._consume()
                backoff = 1.0
            except (pika.exceptions.AMQPError, OSError) as exc:
                self.connected.clear()
                log.warning(
                    "rabbitmq connection lost; reconnecting",
                    extra={"queue": self.queue, "error": repr(exc), "retry_in": backoff},
                )
                self._stop.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        self.connected.clear()

    def _consume(self) -> None:
        connection = pika.BlockingConnection(pika.URLParameters(self._settings.rabbitmq_url))
        try:
            channel = connection.channel()
            channel.basic_qos(prefetch_count=PREFETCH)

            def on_message(
                ch: BlockingChannel, method: Basic.Deliver, properties: BasicProperties, body: bytes
            ) -> None:
                handler = QUEUE_TO_HANDLER.get(self.queue)
                run = (lambda env: handler(self._handlers, env)) if handler else (lambda env: None)
                ack, requeue = _dispose(body, properties, run, QUEUE_MESSAGE_TYPES.get(self.queue))
                if ack:
                    ch.basic_ack(method.delivery_tag)
                else:
                    ch.basic_reject(method.delivery_tag, requeue=requeue)

            channel.basic_consume(self.queue, on_message_callback=on_message)
            self.connected.set()
            log.info("consuming", extra={"queue": self.queue})
            while not self._stop.is_set():
                connection.process_data_events(time_limit=1)
        finally:
            self.connected.clear()
            if connection.is_open:
                connection.close()


def start_consumers(
    settings: Settings, handlers: MessageHandlers, stop: threading.Event, crashed: threading.Event
) -> list[threading.Thread]:
    threads = []
    for queue in settings.broker_consumer_queues:
        consumer = QueueConsumer(queue, settings, handlers, stop)
        thread = threading.Thread(
            target=_guarded(consumer, stop, crashed), name=f"consumer:{queue}", daemon=True
        )
        thread.start()
        threads.append(thread)
    return threads


def _guarded(
    consumer: QueueConsumer, stop: threading.Event, crashed: threading.Event
) -> Callable[[], None]:
    def target() -> None:
        try:
            consumer.run()
        except Exception:  # pylint: disable=broad-exception-caught  # any crash is fatal to this consumer
            log.exception("consumer crashed; stopping", extra={"queue": consumer.queue})
            crashed.set()
            stop.set()

    return target
