"""Integration fixtures: the real shared broker, MySQL, Mailpit and the user-service stub,
with the real worker (python -m app.worker) and API (uvicorn app.main:app) running.

Run inside Compose (docker compose run --rm tests) or from the host with the stack up; the
IT_* variables point at each piece. Messages are published as the broker's admin user,
standing in for event-service, checkout-service and ticket-service.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any

import pika
import pytest

RABBITMQ_URL = os.environ.get(
    "IT_RABBITMQ_URL",
    "amqp://ticketto:ticketto-local@localhost:5672/%2F",  # local-only password; gitleaks:allow
)
STUB_URL = os.environ.get("IT_STUB_URL", "http://localhost:18000")
MAILPIT_URL = os.environ.get("IT_MAILPIT_URL", "http://localhost:8026")
API_URL = os.environ.get("IT_API_URL", "http://localhost:18080")

# message type -> (exchange, routing key, producer), as in rabbitmq-service's messages.yaml
ROUTES = {
    "EventPublished": ("event.events", "event.published", "event-service"),
    "EventCancelled": ("event.events", "event.cancelled", "event-service"),
    "OrderRefunded": ("checkout.events", "order.refunded", "checkout-service"),
    "RefundFailed": ("checkout.events", "refund.failed", "checkout-service"),
    "TicketIssued": ("ticket.events", "ticket.issued", "ticket-service"),
}


def http(method: str, url: str, body: Any = None, headers: dict | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    try:
        return status, json.loads(raw or b"null")
    except ValueError:
        return status, raw


def wait_until(check: Callable[[], Any], what: str, timeout: float = 30) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.5)


def emails_to(address: str) -> list[dict]:
    query = urllib.parse.quote(f'to:"{address}"')
    status, body = http("GET", f"{MAILPIT_URL}/api/v1/search?query={query}")
    assert status == 200, body
    return body["messages"]


def inline_files(message_id: str) -> list[dict]:
    """The inline attachments Mailpit found in one message."""
    status, body = http("GET", f"{MAILPIT_URL}/api/v1/message/{message_id}")
    assert status == 200, body
    return body["Inline"]


class Broker:
    def __init__(self):
        try:
            self.connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
        except pika.exceptions.AMQPError as exc:
            pytest.fail(f"broker not reachable at {RABBITMQ_URL} ({exc}); start rabbitmq-service")
        self.channel = self.connection.channel()
        self.channel.confirm_delivery()

    def publish(self, message_type: str, payload: dict, message_id: str | None = None) -> str:
        exchange, routing_key, producer = ROUTES[message_type]
        message_id = message_id or str(uuid.uuid4())
        body = {
            "message_id": message_id,
            "message_type": message_type,
            "schema_version": 1,
            "occurred_at": "2026-10-25T10:00:00Z",
            "producer": producer,
            "payload": payload,
        }
        self.raw(exchange, routing_key, json.dumps(body).encode(), message_id, message_type)
        return message_id

    def raw(self, exchange, routing_key, body: bytes, message_id=None, message_type=None):
        self.channel.basic_publish(
            exchange,
            routing_key,
            body,
            pika.BasicProperties(
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


@pytest.fixture(scope="session")
def broker():
    b = Broker()
    yield b
    b.connection.close()


@pytest.fixture(scope="session", autouse=True)
def stack_is_up():
    for name, url in (
        ("user-service stub", f"{STUB_URL}/health"),
        ("Mailpit", f"{MAILPIT_URL}/api/v1/info"),
        ("the notifications API", f"{API_URL}/health"),
    ):
        try:
            http("GET", url)
        except OSError as exc:
            pytest.fail(f"{name} not reachable at {url} ({exc}): docker compose up -d --build")


class People:
    """Users in the stub. Each test gets fresh people with unique email addresses."""

    def __init__(self):
        self.users: list[dict] = []

    def add(self, *, notify: bool = True, bought: tuple[str, ...] = ()) -> dict:
        n = uuid.uuid4().hex[:10]
        person = {
            "user_id": str(uuid.uuid4()),
            "cognito_sub": str(uuid.uuid4()),
            "email": f"it-{n}@example.com",
            "name": f"Tester {n[:4]}",
            "notify_new_events": notify,
            "purchased_event_ids": list(bought),
        }
        self.users.append(person)
        status, body = http("PUT", f"{STUB_URL}/_stub/users", self.users)
        assert status == 200, body
        return person


@pytest.fixture
def people():
    yield People()
    http("PUT", f"{STUB_URL}/_stub/users", [])
