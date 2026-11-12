"""Taking messages off notification-service's queues with basic_get.

Each run pulls messages one at a time (rabbitmq-service docs/MESSAGING.md allows basic_get on
your own queues) and settles each one before taking the next:

  handled                    -> basic_ack, only after the database transaction committed
  poison                     -> basic_reject(requeue=False): the queue's DLQ takes it at once
  transient error            -> basic_reject(requeue=True), then stop draining this queue for
                                this run. The broker counts every redelivery and dead-letters
                                a message after 5; retrying straight away would use those up
                                in seconds. Stopping costs one delivery per failed run, and the
                                worker waits longer after one (WORKER_RETRY_SECONDS).

Never basic_nack (on RabbitMQ 4.3 it bypasses the delivery limit), never auto-ack, and never
declare a queue: the broker owns the topology.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import pika

from .correlation import bound_correlation_id
from .messages import Message, PoisonMessage, parse

logger = logging.getLogger(__name__)

QUEUES = (
    "notification-service.EventPublished",
    "notification-service.EventCancelled",
    "notification-service.OrderRefunded",
    "notification-service.RefundFailed",
    "notification-service.TicketIssued",
)


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
        with bound_correlation_id(correlation_id):
            logger.error("poison message dead-lettered", extra={"reason": str(exc)})
        return Disposition.DEAD_LETTER
    if message.payload is None:
        return Disposition.ACK  # a message type this service doesn't use
    try:
        run(message)
    except Exception:  # pylint: disable=broad-exception-caught  # retried via requeue
        with bound_correlation_id(message.correlation_id or message.message_id):
            logger.exception(
                "message handling failed; will be retried",
                extra={
                    "message_id": message.message_id,
                    "message_type": message.message_type,
                    "attempt": attempt,
                },
            )
        return Disposition.REQUEUE
    return Disposition.ACK


@dataclass
class DrainResult:
    acked: int = 0
    dead_lettered: int = 0
    requeued: int = 0
    hit_deadline: bool = False  # stopped for time, so there may be more on the queue

    @property
    def taken(self) -> int:
        """Messages removed from the queue."""
        return self.acked + self.dead_lettered


def drain(
    channel: Any,
    queue: str,
    run: Callable[[Message], object],
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> DrainResult:
    """Settle messages from `queue` until it is empty, `deadline` (clock time) passes, or a
    message fails transiently."""
    result = DrainResult()
    while clock() < deadline:
        method, properties, body = channel.basic_get(queue, auto_ack=False)
        if method is None:
            break
        disposition = dispose(
            body,
            run,
            correlation_id=getattr(properties, "correlation_id", None),
            attempt=_attempt(properties),
        )
        if disposition is Disposition.ACK:
            channel.basic_ack(method.delivery_tag)
            result.acked += 1
        elif disposition is Disposition.DEAD_LETTER:
            channel.basic_reject(method.delivery_tag, requeue=False)
            result.dead_lettered += 1
        else:
            channel.basic_reject(method.delivery_tag, requeue=True)
            result.requeued += 1
            break
    else:
        result.hit_deadline = True
    return result


def connect(url: str) -> pika.BlockingConnection:
    params = pika.URLParameters(url)
    params.connection_attempts = 1
    params.socket_timeout = 5
    params.blocked_connection_timeout = 10
    params.heartbeat = 60
    return pika.BlockingConnection(params)
