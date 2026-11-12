"""The worker against the real shared broker: OrderConfirmed and EventCancelled end to end.

Needs the broker (../rabbitmq-service: docker compose up -d --build) and this stack with the
worker (docker compose --profile worker up -d). Run from the host: pytest -m broker
Messages are published as the broker's admin user, standing in for checkout-service and
event-service.
"""

import json
import os
import time
import uuid

import pika
import pytest
from sqlalchemy import text

pytestmark = pytest.mark.broker

RABBITMQ_URL = os.environ.get(
    "IT_RABBITMQ_URL",
    "amqp://ticketto:ticketto-local@localhost:5672/%2F",  # local-only password; gitleaks:allow
)


@pytest.fixture(scope="module")
def channel():
    try:
        connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
    except pika.exceptions.AMQPError as exc:
        pytest.fail(f"broker not reachable at {RABBITMQ_URL} ({exc}); start rabbitmq-service")
    ch = connection.channel()
    ch.confirm_delivery()
    yield ch
    connection.close()


# message type -> (exchange, routing key, producer), as in rabbitmq-service's messages.yaml
ROUTES = {
    "OrderConfirmed": ("checkout.events", "order.confirmed", "checkout-service"),
    "EventCancelled": ("event.events", "event.cancelled", "event-service"),
}


def _publish(ch, message_type: str, payload: dict) -> None:
    exchange, routing_key, producer = ROUTES[message_type]
    message_id = str(uuid.uuid4())
    body = {
        "message_id": message_id,
        "message_type": message_type,
        "schema_version": 1,
        "occurred_at": "2026-10-25T10:00:00Z",
        "producer": producer,
        "payload": payload,
    }
    ch.basic_publish(
        exchange,
        routing_key,
        json.dumps(body).encode(),
        pika.BasicProperties(
            content_type="application/json",
            delivery_mode=pika.DeliveryMode.Persistent,
            message_id=message_id,
            type=message_type,
            correlation_id=str(uuid.uuid4()),
        ),
        mandatory=True,
    )


def _order_confirmed(ch, sub: str, event_id: str) -> None:
    payload = {
        "order_id": str(uuid.uuid4()),
        "hold_id": str(uuid.uuid4()),
        "admission_id": str(uuid.uuid4()),
        "user_id": sub,
        "event_id": event_id,
        "category_id": str(uuid.uuid4()),
        "quantity": 1,
        "delivery_email": "it@test.com",
        "total_amount": 8800,
        "currency": "SGD",
        "confirmed_at": "2026-10-25T10:00:00Z",
    }
    _publish(ch, "OrderConfirmed", payload)


def _event_cancelled(ch, event_id: str) -> None:
    payload = {
        "event_id": event_id,
        "event_version": 3,
        "cancellation_id": str(uuid.uuid4()),
        "cancelled_at": "2026-10-25T10:00:00Z",
        "reason": "Integration test.",
    }
    _publish(ch, "EventCancelled", payload)


@pytest.fixture
def two_users(db_engine):
    users = [(str(uuid.uuid4()), str(uuid.uuid4())) for _ in range(2)]
    with db_engine.begin() as conn:
        for user_id, sub in users:
            conn.execute(
                text(
                    "INSERT INTO users (user_id, cognito_sub, email, name) "
                    "VALUES (:id, :sub, :email, 'Worker IT')"
                ),
                {"id": user_id, "sub": sub, "email": f"it-{sub[:8]}@test.com"},
            )
    yield users
    with db_engine.begin() as conn:
        for user_id, _ in users:
            conn.execute(text("DELETE FROM users WHERE user_id = :id"), {"id": user_id})


# A new connection per read: under REPEATABLE READ one connection keeps seeing its first snapshot.
def _purchases(db_engine, user_id: str) -> str | None:
    with db_engine.connect() as conn:
        return conn.execute(
            text("SELECT purchased_event_ids FROM users WHERE user_id = :id"), {"id": user_id}
        ).scalar()


def _is_cancelled(db_engine, event_id: str) -> bool:
    with db_engine.connect() as conn:
        return bool(
            conn.execute(
                text("SELECT 1 FROM cancelled_events WHERE event_id = :e"), {"e": event_id}
            ).scalar()
        )


def _wait_for(check, what: str, timeout: float = 15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}")


def test_purchase_then_cancel_then_late_purchase(api, channel, db_engine, two_users):
    (buyer_id, buyer_sub), (late_id, late_sub) = two_users
    event_id = str(uuid.uuid4())

    _order_confirmed(channel, buyer_sub, event_id)
    _wait_for(lambda: _purchases(db_engine, buyer_id) == event_id, "the purchase to be recorded")

    _event_cancelled(channel, event_id)
    _wait_for(lambda: _is_cancelled(db_engine, event_id), "the cancellation to be recorded")

    _order_confirmed(channel, late_sub, event_id)  # arrives after the cancellation
    marker_event = str(uuid.uuid4())
    _order_confirmed(channel, late_sub, marker_event)  # same queue, processed after it
    _wait_for(lambda: _purchases(db_engine, late_id) == marker_event, "the marker purchase")

    # The buyer keeps the cancelled event, so notification-service can still email them.
    assert _purchases(db_engine, buyer_id) == event_id
    res = api.get("/internal/v1/users", params={"purchased_event_id": event_id})
    assert [u["user_id"] for u in res.json()["items"]] == [buyer_id]


def test_dead_letter_queues_are_empty(channel):
    for queue in ("user-service.OrderConfirmed.dlq", "user-service.EventCancelled.dlq"):
        assert channel.queue_declare(queue, passive=True).method.message_count == 0, queue
