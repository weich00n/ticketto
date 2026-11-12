"""Topology against real RabbitMQ: idempotent declaration, routing, dead-lettering,
and how many attempts x-delivery-limit really allows."""

from __future__ import annotations

import time

import pika.exceptions
import pytest
from pika.adapters.blocking_connection import BlockingChannel

from app.messaging.topology import (
    EVENT_CANCELLED,
    EVENT_PUBLISHED,
    EVENT_SOLD_OUT,
    MAX_ATTEMPTS,
    ORDER_CONFIRMED,
    ConsumerQueue,
    check_queues,
    declare_topology,
)
from tests.fakes.checkout_service import FakeCheckoutService
from tests.fakes.event_service import FakeEventService
from tests.fakes.reservation_service import FakeReservationService

pytestmark = pytest.mark.integration


def _get(channel: BlockingChannel, queue: str, timeout: float = 5.0) -> tuple[object, bytes]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        method, _props, body = channel.basic_get(queue)
        if method is not None:
            return method, body
        time.sleep(0.05)
    raise AssertionError(f"nothing arrived on {queue}")


def _empty(channel: BlockingChannel, queue: str) -> bool:
    method, _, _ = channel.basic_get(queue, auto_ack=False)
    if method is not None:
        channel.basic_reject(method.delivery_tag, requeue=True)
        return False
    return True


def test_declaring_twice_is_fine(rabbit: BlockingChannel) -> None:
    declare_topology(rabbit)
    declare_topology(rabbit)


def test_check_queues_passes_once_the_queues_exist(rabbit: BlockingChannel) -> None:
    check_queues(rabbit)  # the fixture declared the topology


def test_check_queues_fails_on_a_missing_queue(rabbit: BlockingChannel) -> None:
    with pytest.raises(pika.exceptions.ChannelClosedByBroker) as exc:
        rabbit.queue_declare("queue-service.does-not-exist", passive=True)
    assert exc.value.reply_code == 404  # what check_queues reports as "queue missing"


@pytest.mark.parametrize(
    ("service", "routing_key", "queue"),
    [
        (FakeEventService, "event.published", EVENT_PUBLISHED),
        (FakeEventService, "event.cancelled", EVENT_CANCELLED),
        (FakeReservationService, "event.sold_out", EVENT_SOLD_OUT),
        (FakeCheckoutService, "order.confirmed", ORDER_CONFIRMED),
    ],
)
def test_routing(
    rabbit: BlockingChannel, service: type, routing_key: str, queue: ConsumerQueue
) -> None:
    service(rabbit).publish_raw(routing_key, b"{}")
    method, body = _get(rabbit, queue.name)
    rabbit.basic_ack(method.delivery_tag)  # type: ignore[attr-defined]
    assert body == b"{}"


def test_event_updated_is_not_consumed(rabbit: BlockingChannel) -> None:
    """No EventUpdated queue on the shared broker, so none locally either."""
    with pytest.raises(pika.exceptions.UnroutableError):
        FakeEventService(rabbit).publish_raw("event.updated", b"{}")


def test_unrelated_routing_keys_are_not_consumed(rabbit: BlockingChannel) -> None:
    """Published as mandatory with confirms: the broker returns it as unroutable."""
    with pytest.raises(pika.exceptions.UnroutableError):
        FakeReservationService(rabbit).publish_raw("seat.held", b"{}")
    assert _empty(rabbit, EVENT_SOLD_OUT.name)


@pytest.mark.parametrize(
    "queue",
    [EVENT_PUBLISHED, EVENT_CANCELLED, EVENT_SOLD_OUT, ORDER_CONFIRMED],
    ids=lambda q: q.name,
)
def test_rejected_message_is_dead_lettered(rabbit: BlockingChannel, queue: ConsumerQueue) -> None:
    rabbit.basic_publish("", queue.name, b"poison")  # default exchange: straight to the queue
    method, _ = _get(rabbit, queue.name)
    rabbit.basic_nack(method.delivery_tag, requeue=False)  # type: ignore[attr-defined]

    dead, body = _get(rabbit, queue.dlq)
    rabbit.basic_ack(dead.delivery_tag)  # type: ignore[attr-defined]
    assert body == b"poison"


def test_failing_message_is_dead_lettered_after_max_attempts(rabbit: BlockingChannel) -> None:
    """Confirms the reading of x-delivery-limit: limit 4 = 5 attempts in total.

    Quorum queues apply a requeue (nack) asynchronously, so an empty basic_get
    right after a nack doesn't mean the message was dead-lettered. Poll both the
    queue and its DLQ until the message shows up in one of them.
    """
    rabbit.basic_publish("", EVENT_PUBLISHED.name, b"keeps failing")
    attempts = 0
    dead_body: bytes | None = None
    deadline = time.monotonic() + 15
    while dead_body is None:
        assert time.monotonic() < deadline, f"lost track of the message after {attempts} attempts"
        method, _, _ = rabbit.basic_get(EVENT_PUBLISHED.name)
        if method is not None:
            attempts += 1
            assert attempts <= MAX_ATTEMPTS + 2, f"not dead-lettered after {attempts} attempts"
            rabbit.basic_reject(method.delivery_tag, requeue=True)
            continue
        dead, _, body = rabbit.basic_get(EVENT_PUBLISHED.dlq, auto_ack=True)
        if dead is not None:
            dead_body = body
        else:
            time.sleep(0.05)  # requeue or dead-letter still in flight

    assert dead_body == b"keeps failing"
    assert attempts == MAX_ATTEMPTS, f"dead-lettered after {attempts} attempts"
