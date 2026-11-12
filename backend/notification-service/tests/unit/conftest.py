"""Unit-test fixtures: SQLite instead of MySQL, and fakes for user-service, event-service,
the email sender and the broker channel."""

import json
import uuid
from collections import deque
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import Base, create_db_engine, create_session_factory
from app.models import Notification
from app.user_client import Recipient, UserNotFound, UserServiceUnavailable
from tests.payloads import (
    event_published_payload,
    order_refunded_payload,
    refund_failed_payload,
    ticket_issued_payload,
)

EVENT = "a14ec682-62f5-46db-b94a-a8d95435e274"
OTHER_EVENT = "b05e8625-6d43-4252-ad2c-bda3222a769b"


def sub(n: int) -> str:
    return f"10000000-0000-4000-8000-{n:012d}"


def user(n: int) -> Recipient:
    return Recipient(
        user_id=f"00000000-0000-4000-8000-{n:012d}",
        email=f"u{n}@example.com",
        name=f"User {n}",
        cognito_sub=sub(n),
    )


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'notification.db'}",
        rabbitmq_url="amqp://notification-service:test@broker.test:5672/%2F",
        user_service_url="http://user-service.test:8000",
        event_service_url="http://event-service.test:3001",
        email_backend="log",
        email_from="Ticketto <notifications@ticketto.test>",
    )


@pytest.fixture
def session_factory(settings):
    engine = create_db_engine(settings)
    Base.metadata.create_all(engine)
    yield create_session_factory(settings, engine=engine)
    engine.dispose()


@dataclass
class FakeUsers:
    """Stands in for app.user_client.UserClient."""

    opted_in_users: list[Recipient] = field(default_factory=list)
    buyers_by_event: dict[str, list[Recipient]] = field(default_factory=dict)
    users_by_sub: dict[str, Recipient] = field(default_factory=dict)
    down: bool = False
    calls: list[tuple] = field(default_factory=list)

    def _check(self):
        if self.down:
            raise UserServiceUnavailable("simulated outage")

    def opted_in(self) -> list[Recipient]:
        self.calls.append(("opted_in",))
        self._check()
        return list(self.opted_in_users)

    def buyers(self, event_id: str) -> list[Recipient]:
        self.calls.append(("buyers", event_id))
        self._check()
        return list(self.buyers_by_event.get(event_id, []))

    def by_sub(self, cognito_sub: str) -> Recipient:
        self.calls.append(("by_sub", cognito_sub))
        self._check()
        if cognito_sub not in self.users_by_sub:
            raise UserNotFound(cognito_sub)
        return self.users_by_sub[cognito_sub]


@pytest.fixture
def users() -> FakeUsers:
    return FakeUsers()


@dataclass
class FakeEvents:
    """Stands in for app.event_client.EventClient."""

    details_by_event: dict[str, dict] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def details(self, event_id: str):
        self.calls.append(event_id)
        found = self.details_by_event.get(event_id)
        return SimpleNamespace(**found) if found else None


@pytest.fixture
def events() -> FakeEvents:
    return FakeEvents()


@dataclass
class FakeSender:
    """Stands in for an EmailSender. fail_next=n makes the next n sends raise."""

    sent: list = field(default_factory=list)
    fail_next: int = 0

    def send(self, email) -> None:
        if self.fail_next:
            self.fail_next -= 1
            raise OSError("smtp down")
        self.sent.append(email)


@pytest.fixture
def sender() -> FakeSender:
    return FakeSender()


# ---------- messages, shaped like the publishers' (rabbitmq-service contracts/examples) ----------


def envelope(message_type: str, payload: dict, **overrides) -> bytes:
    producer = {"OrderRefunded": "checkout-service", "TicketIssued": "ticket-service"}.get(
        message_type, "event-service"
    )
    body = {
        "message_id": str(uuid.uuid4()),
        "message_type": message_type,
        "schema_version": 1,
        "occurred_at": "2026-10-20T08:00:00Z",
        "producer": producer,
        "payload": payload,
    }
    body.update(overrides)
    return json.dumps(body).encode()


def event_published(event_id: str = EVENT, title: str = "Autumn Live", **overrides) -> bytes:
    return envelope("EventPublished", event_published_payload(event_id, title), **overrides)


def event_cancelled(event_id: str = EVENT, reason: str = "Venue unavailable.", **overrides):
    payload = {
        "event_id": event_id,
        "event_version": 3,
        "cancellation_id": str(uuid.uuid4()),
        "cancelled_at": "2026-10-21T11:00:00Z",
        "reason": reason,
    }
    return envelope("EventCancelled", payload, **overrides)


def order_refunded(user_sub: str, event_id: str = EVENT, **payload_overrides) -> bytes:
    payload = {**order_refunded_payload(user_sub, event_id), **payload_overrides}
    return envelope("OrderRefunded", payload)


def refund_failed(user_sub: str, event_id: str = EVENT, **payload_overrides) -> bytes:
    payload = {**refund_failed_payload(user_sub, event_id), **payload_overrides}
    return envelope("RefundFailed", payload)


def ticket_issued(user_sub: str, event_id: str = EVENT, **payload_overrides) -> bytes:
    payload = {**ticket_issued_payload(user_sub, event_id), **payload_overrides}
    return envelope("TicketIssued", payload)


def ticket_context(tickets: int = 2) -> dict:
    """What a queued ticket email's row holds, before its QR codes are removed."""
    return {
        "title": "Autumn Live",
        "venue": "National Stadium",
        "starts_at": "2026-11-20T12:00:00Z",
        "category_name": "CAT 1",
        "order_id": "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b",
        "tickets": [
            {
                "ticket_id": f"6f1e2d3c-4b5a-4978-8695-a4b3c2d1e0f{n}",
                "ticket_number": n,
                "qr_payload": f"TKT1.k1.6f1e2d3c-4b5a-4978-8695-a4b3c2d1e0f{n}.{'a' * 43}",
            }
            for n in range(1, tickets + 1)
        ],
    }


def notifications(session_factory, **filters) -> list[Notification]:
    with session_factory() as session:
        query = select(Notification).order_by(Notification.id)
        for name, value in filters.items():
            query = query.where(getattr(Notification, name) == value)
        return list(session.scalars(query))


# ---------- a fake pika channel for basic_get draining ----------


class FakeChannel:
    """Only basic_get, basic_ack and basic_reject exist: anything else (declaring a queue,
    basic_consume, basic_nack) raises AttributeError, so the test fails."""

    def __init__(self, queues: dict[str, list[bytes]] | None = None):
        self.queues: dict[str, deque] = {q: deque(b) for q, b in (queues or {}).items()}
        self.calls: list[tuple] = []
        self._tags = 0
        self._outstanding: dict[int, tuple[str, bytes]] = {}

    def basic_get(self, queue: str, auto_ack: bool = False):
        assert auto_ack is False, "auto-ack loses messages"
        self.calls.append(("get", queue))
        pending = self.queues.get(queue)
        if not pending:
            return None, None, None
        body = pending.popleft()
        self._tags += 1
        self._outstanding[self._tags] = (queue, body)
        method = SimpleNamespace(delivery_tag=self._tags, redelivered=False)
        properties = SimpleNamespace(
            headers=None, correlation_id=str(uuid.uuid4()), message_id=None
        )
        return method, properties, body

    def basic_ack(self, delivery_tag):
        self.calls.append(("ack", delivery_tag))
        self._outstanding.pop(delivery_tag)

    def basic_reject(self, delivery_tag, requeue=True):
        self.calls.append(("reject", delivery_tag, requeue))
        queue, body = self._outstanding.pop(delivery_tag)
        if requeue:
            self.queues[queue].appendleft(body)  # back at the head, as RabbitMQ does


class FakeClock:
    """A monotonic clock the test moves by hand; each read can also advance it."""

    def __init__(self, start: float = 1000.0, step: float = 0.0):
        self.now = start
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value
