"""DLQ inspection and replay against real RabbitMQ."""

from __future__ import annotations

import time

import pika
import pytest
from pika.adapters.blocking_connection import BlockingChannel

from app.messaging.topology import EVENT_PUBLISHED, EVENT_SOLD_OUT
from workers.dlq import depths, peek, replay

pytestmark = pytest.mark.integration


def _dead_letter(channel: BlockingChannel, body: bytes, message_id: str) -> None:
    """Get a message into the EventPublished DLQ the real way: reject it."""
    channel.basic_publish(
        "",
        EVENT_PUBLISHED.name,
        body,
        properties=pika.BasicProperties(message_id=message_id, type="EventPublished"),
    )
    for _ in range(100):
        method, _, _ = channel.basic_get(EVENT_PUBLISHED.name)
        if method is not None:
            channel.basic_nack(method.delivery_tag, requeue=False)
            return
        time.sleep(0.05)
    raise AssertionError("message never arrived")


def _wait_depth(channel: BlockingChannel, short: str, expected: int) -> None:
    for _ in range(100):
        if depths(channel)[short] == expected:
            return
        time.sleep(0.05)
    raise AssertionError(f"{short} DLQ never reached depth {expected}: {depths(channel)}")


def test_list_and_peek_leave_messages_in_place(rabbit: BlockingChannel) -> None:
    for i in range(3):
        _dead_letter(rabbit, f"msg-{i}".encode(), f"id-{i}")
    _wait_depth(rabbit, "EventPublished", 3)
    assert depths(rabbit)["EventSoldOut"] == 0

    seen = peek(rabbit, EVENT_PUBLISHED, limit=2)

    assert [(d.message_id, d.message_type, d.body, d.reason) for d in seen] == [
        ("id-0", "EventPublished", b"msg-0", "rejected"),
        ("id-1", "EventPublished", b"msg-1", "rejected"),
    ]
    _wait_depth(rabbit, "EventPublished", 3)  # nothing removed (order may change)


def test_replay_moves_oldest_first_and_strips_broker_headers(rabbit: BlockingChannel) -> None:
    for i in range(3):
        _dead_letter(rabbit, f"msg-{i}".encode(), f"id-{i}")
    _wait_depth(rabbit, "EventPublished", 3)

    assert replay(rabbit, EVENT_PUBLISHED, limit=2) == 2
    _wait_depth(rabbit, "EventPublished", 1)

    replayed = []
    for _ in range(2):
        method = None
        for _ in range(100):
            method, properties, body = rabbit.basic_get(EVENT_PUBLISHED.name, auto_ack=True)
            if method is not None:
                break
            time.sleep(0.05)
        assert method is not None
        replayed.append((properties.message_id, properties.type, body))
        assert "x-death" not in (properties.headers or {})
    assert replayed == [("id-0", "EventPublished", b"msg-0"), ("id-1", "EventPublished", b"msg-1")]


def test_replay_empty_dlq(rabbit: BlockingChannel) -> None:
    assert replay(rabbit, EVENT_SOLD_OUT) == 0
