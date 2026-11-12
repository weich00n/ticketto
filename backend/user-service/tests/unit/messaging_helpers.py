"""Builders for broker messages and a fake pika channel, shared by the consumer tests."""

import json
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

EVENT = "a14ec682-62f5-46db-b94a-a8d95435e274"
OTHER_EVENT = "b05e8625-6d43-4252-ad2c-bda3222a769b"


def envelope(message_type: str, payload: dict, **overrides) -> bytes:
    body = {
        "message_id": str(uuid.uuid4()),
        "message_type": message_type,
        "schema_version": 1,
        "occurred_at": "2026-10-20T08:00:00Z",
        "producer": "checkout-service" if message_type == "OrderConfirmed" else "event-service",
        "payload": payload,
    }
    body.update(overrides)
    return json.dumps(body).encode()


def order_confirmed(user_sub: str, event_id: str = EVENT, **overrides) -> bytes:
    # The fields checkout-service sends (checkout-service README, "Published").
    payload = {
        "order_id": str(uuid.uuid4()),
        "hold_id": str(uuid.uuid4()),
        "admission_id": str(uuid.uuid4()),
        "user_id": user_sub,
        "event_id": event_id,
        "category_id": str(uuid.uuid4()),
        "quantity": 2,
        "delivery_email": "alice@example.com",
        "total_amount": 17600,
        "currency": "SGD",
        "confirmed_at": "2026-10-20T08:00:00Z",
    }
    return envelope("OrderConfirmed", payload, **overrides)


def event_cancelled(event_id: str = EVENT, **overrides) -> bytes:
    payload = {
        "event_id": event_id,
        "event_version": 3,
        "cancellation_id": str(uuid.uuid4()),
        "cancelled_at": "2026-10-21T11:00:00Z",
        "reason": "Event cancelled by organiser.",
    }
    return envelope("EventCancelled", payload, **overrides)


@dataclass
class FakeChannel:
    """Records what a consumer did with each delivery."""

    calls: list[tuple] = field(default_factory=list)

    def basic_ack(self, delivery_tag):
        self.calls.append(("ack", delivery_tag))

    def basic_reject(self, delivery_tag, requeue=True):
        self.calls.append(("reject", delivery_tag, requeue))

    def basic_nack(self, *args, **kwargs):  # must never be used: it bypasses the delivery limit
        raise AssertionError("basic_nack must not be used")


def delivery(tag: int = 1, redeliveries: int = 0):
    method = SimpleNamespace(delivery_tag=tag)
    properties = SimpleNamespace(
        headers={"x-delivery-count": redeliveries} if redeliveries else None,
        correlation_id=str(uuid.uuid4()),
        message_id=None,
    )
    return method, properties
