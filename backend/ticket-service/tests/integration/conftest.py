import json
import os
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pika
import pytest

SERVICE_URL = os.environ.get("IT_SERVICE_URL", "http://127.0.0.1:3006")
# The broker's admin user: the tests publish as Checkout and Event would and read DLQs, which
# Ticket's own user may not do.
RABBITMQ_URL = os.environ.get(
    "IT_RABBITMQ_URL",
    "amqp://ticketto:ticketto-local@127.0.0.1:5672/%2F",  # gitleaks:allow
)
# Where the tests read what Ticket publishes: a queue of their own on Ticket's exchange, as
# notification-service's queue will be on the team broker.
TAP_QUEUE = "it.TicketIssued"

BUYER = "2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33"
# No authentication yet (AUTH_MODE=none): buyers are named by X-User-Id; check-in is open.
STAFF = {"X-User-Id": "gate-it"}


def buyer(user_id: str = BUYER) -> dict[str, str]:
    return {"X-User-Id": user_id}


def request(
    method: str, path: str, *, headers: dict | None = None, body: Any = None, base=SERVICE_URL
) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{base}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw, status = error.read(), error.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def wait_until(check: Callable[[], Any], timeout: float = 20, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.25)


def iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Publisher:
    def __init__(self):
        self.connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
        self.channel = self.connection.channel()
        self.channel.confirm_delivery()

    def publish(
        self,
        exchange: str,
        routing_key: str,
        message_type: str,
        payload: dict,
        message_id: str | None = None,
    ) -> str:
        message_id = message_id or str(uuid.uuid4())
        body = {
            "message_id": message_id,
            "message_type": message_type,
            "schema_version": 1,
            "occurred_at": iso_now(),
            "producer": "integration-test",
            "payload": payload,
        }
        self.raw(exchange, routing_key, json.dumps(body).encode(), message_id, message_type)
        return message_id

    def raw(self, exchange, routing_key, body: bytes, message_id=None, message_type=None):
        self.channel.basic_publish(
            exchange=exchange,
            routing_key=routing_key,
            body=body,
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=pika.DeliveryMode.Persistent,
                message_id=message_id,
                type=message_type,
                correlation_id=str(uuid.uuid4()),
            ),
            mandatory=True,
        )

    def message_count(self, queue: str) -> int:
        return self.channel.queue_declare(queue, passive=True).method.message_count

    def close(self):
        self.connection.close()


@pytest.fixture(scope="session")
def publisher():
    pub = Publisher()
    yield pub
    pub.close()


def _order_of(body) -> str | None:
    """The order a TicketIssued is for; None for anything else on the queue (e.g. the
    permission test's empty message)."""
    payload = body.get("payload") if isinstance(body, dict) else None
    return payload.get("order_id") if isinstance(payload, dict) else None


class Tap:
    """Collects every TicketIssued the worker publishes (bound before any test runs)."""

    def __init__(self):
        self.connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
        self.channel = self.connection.channel()
        # Exclusive: RabbitMQ 4.3 refuses transient queues that aren't, and the broker deletes
        # it when the tests disconnect.
        self.channel.queue_declare(TAP_QUEUE, exclusive=True)
        self.channel.queue_bind(TAP_QUEUE, "ticket.events", routing_key="ticket.issued")
        self.seen: list[tuple[pika.BasicProperties, dict]] = []

    def find(self, order_id: str, timeout: float = 20) -> tuple[pika.BasicProperties, dict]:
        def check():
            while True:
                method, properties, body = self.channel.basic_get(TAP_QUEUE, auto_ack=True)
                if method is None:
                    break
                self.seen.append((properties, json.loads(body)))
            matches = [m for m in self.seen if _order_of(m[1]) == order_id]
            return matches[-1] if matches else None

        return wait_until(check, timeout, what=f"TicketIssued for {order_id}")

    def close(self):
        self.connection.close()  # removes the exclusive queue


@pytest.fixture(scope="session")
def ticket_issued():
    tap = Tap()
    yield tap
    tap.close()


def order_payload(**overrides) -> dict:
    payload = {
        "order_id": str(uuid.uuid4()),
        "hold_id": str(uuid.uuid4()),
        "admission_id": str(uuid.uuid4()),
        "user_id": BUYER,
        "event_id": str(uuid.uuid4()),
        "category_id": str(uuid.uuid4()),
        "quantity": 2,
        "delivery_email": f"buyer+{uuid.uuid4().hex[:8]}@example.com",
        "confirmed_at": iso_now(),
    }
    payload.update(overrides)
    return payload


def tickets_for(order_id: str, user_id: str = BUYER) -> list[dict]:
    status, body = request("GET", f"/api/v1/tickets?order_id={order_id}", headers=buyer(user_id))
    assert status == 200, body
    return body["items"]
