"""RabbitMQ topology.

Locally (RABBITMQ_DECLARE_TOPOLOGY=true, the default) the consumer declares all of
it idempotently at startup, including the other services' exchanges, with the type
and durability they use, so re-declaring never conflicts. On the shared broker
(false) the broker owners create it, and the consumer only checks that its own
queues exist (`check_queues`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # keeps pika out of the worker healthcheck's imports
    from pika.adapters.blocking_connection import BlockingChannel

DLX = "ticketto.dlx"  # same name as on the shared broker
# x-delivery-limit counts *redeliveries*: a message is dead-lettered once it has
# been delivered unsuccessfully more than this many times. 4 means 5 attempts.
DELIVERY_LIMIT = 4
MAX_ATTEMPTS = DELIVERY_LIMIT + 1

# exchange -> owner (all topic, durable)
EXCHANGES = {
    "event.events": "event-service",
    "reservation.events": "reservation-service",
    "checkout.events": "checkout-service",
    "queue.events": "queue-service",
}


@dataclass(frozen=True)
class ConsumerQueue:
    name: str
    exchange: str
    routing_key: str

    @property
    def dlq(self) -> str:
        return f"{self.name}.dlq"


# One queue per consumed message type, named as on the shared broker. The bindings
# are only used locally (RABBITMQ_DECLARE_TOPOLOGY=true); the broker owns them there.
# EventUpdated isn't consumed: there's no queue for it, so none here either.
EVENT_PUBLISHED = ConsumerQueue("queue-service.EventPublished", "event.events", "event.published")
EVENT_CANCELLED = ConsumerQueue("queue-service.EventCancelled", "event.events", "event.cancelled")
EVENT_SOLD_OUT = ConsumerQueue("queue-service.EventSoldOut", "reservation.events", "event.sold_out")
ORDER_CONFIRMED = ConsumerQueue(
    "queue-service.OrderConfirmed", "checkout.events", "order.confirmed"
)
CONSUMER_QUEUES = (EVENT_PUBLISHED, EVENT_CANCELLED, EVENT_SOLD_OUT, ORDER_CONFIRMED)


def queue_arguments(queue: ConsumerQueue) -> dict[str, Any]:
    return {
        "x-queue-type": "quorum",
        "x-dead-letter-exchange": DLX,
        "x-dead-letter-routing-key": queue.dlq,
        "x-delivery-limit": DELIVERY_LIMIT,
    }


def check_queues(channel: BlockingChannel) -> None:
    """Passively check Queue's consumer queues exist; declares nothing.
    A missing queue closes the channel with 404 NOT_FOUND."""
    for queue in CONSUMER_QUEUES:
        channel.queue_declare(queue.name, passive=True)


def declare_topology(channel: BlockingChannel) -> None:
    for exchange in EXCHANGES:
        channel.exchange_declare(exchange, exchange_type="topic", durable=True)
    channel.exchange_declare(DLX, exchange_type="direct", durable=True)
    for queue in CONSUMER_QUEUES:
        channel.queue_declare(queue.dlq, durable=True, arguments={"x-queue-type": "quorum"})
        channel.queue_bind(queue.dlq, DLX, routing_key=queue.dlq)
        channel.queue_declare(queue.name, durable=True, arguments=queue_arguments(queue))
        channel.queue_bind(queue.name, queue.exchange, routing_key=queue.routing_key)
