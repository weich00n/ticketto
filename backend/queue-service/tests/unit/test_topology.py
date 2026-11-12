"""What declare_topology asks the broker for, recorded by a stub channel.
tests/integration/messaging/test_topology.py checks it against real RabbitMQ."""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.messaging.topology import (
    CONSUMER_QUEUES,
    DELIVERY_LIMIT,
    DLX,
    MAX_ATTEMPTS,
    check_queues,
    declare_topology,
)

pytestmark = pytest.mark.unit


class RecordingChannel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return record


@pytest.fixture
def calls() -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
    channel = RecordingChannel()
    declare_topology(cast(Any, channel))
    return channel.calls


def test_exchanges(calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]]) -> None:
    exchanges = {args[0]: kw for name, args, kw in calls if name == "exchange_declare"}
    for name in ("event.events", "reservation.events", "checkout.events", "queue.events"):
        assert exchanges[name] == {"exchange_type": "topic", "durable": True}
    assert exchanges[DLX] == {"exchange_type": "direct", "durable": True}


def test_consumer_queues_are_quorum_with_dlx_and_delivery_limit(
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]],
) -> None:
    queues = {args[0]: kw for name, args, kw in calls if name == "queue_declare"}
    for queue in CONSUMER_QUEUES:
        assert queues[queue.name] == {
            "durable": True,
            "arguments": {
                "x-queue-type": "quorum",
                "x-dead-letter-exchange": DLX,
                "x-dead-letter-routing-key": f"{queue.name}.dlq",
                "x-delivery-limit": DELIVERY_LIMIT,
            },
        }
        assert queues[queue.dlq] == {"durable": True, "arguments": {"x-queue-type": "quorum"}}


def test_bindings(calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]]) -> None:
    bindings = {
        (args[0], args[1], kw["routing_key"]) for name, args, kw in calls if name == "queue_bind"
    }
    assert bindings == {
        ("queue-service.EventPublished", "event.events", "event.published"),
        ("queue-service.EventCancelled", "event.events", "event.cancelled"),
        ("queue-service.EventSoldOut", "reservation.events", "event.sold_out"),
        ("queue-service.OrderConfirmed", "checkout.events", "order.confirmed"),
        ("queue-service.EventPublished.dlq", DLX, "queue-service.EventPublished.dlq"),
        ("queue-service.EventCancelled.dlq", DLX, "queue-service.EventCancelled.dlq"),
        ("queue-service.EventSoldOut.dlq", DLX, "queue-service.EventSoldOut.dlq"),
        ("queue-service.OrderConfirmed.dlq", DLX, "queue-service.OrderConfirmed.dlq"),
    }


def test_check_queues_only_passively_checks_our_queues() -> None:
    channel = RecordingChannel()
    check_queues(cast(Any, channel))
    assert channel.calls == [
        ("queue_declare", (queue.name,), {"passive": True}) for queue in CONSUMER_QUEUES
    ]


def test_five_attempts_before_dead_lettering() -> None:
    assert MAX_ATTEMPTS == 5
