# pylint: disable=protected-access  # the test helpers drive the service's own internals
"""Shared fixtures: a real in-memory SQLite DB plus fakes for the broker, reservation
and Stripe, wired into a Flask app.

The outbox is a fake that records published messages in order instead of talking to
RabbitMQ, so the tests assert exactly which events each transition emits. The DB is the
real SQLAlchemy schema (all tables created from the models), so the state machines and
their constraints are exercised for real.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import selectinload, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.main import create_app
from app.messaging.envelope import Envelope, parse
from app.messaging.outbox import Outbox
from app.models import Order, OutboxMessage
from app.service import CheckoutService
from tests.fakes import FakeReservation, FakeStripe

# --------------------------------------------------------------------- fakes


class FakeOutbox(Outbox):
    """Records published messages instead of publishing them to the broker.

    `write` is inherited (it stages rows in the caller's transaction); only
    `deliver_pending` is overridden to capture and mark delivered.
    """

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.published: list[tuple[str, dict]] = []  # (routing_key, envelope)

    def _connect(self):  # pragma: no cover - must never be called in tests
        raise AssertionError("real broker connection attempted in a test")

    def deliver_pending(self, db, limit: int = 100) -> int:
        rows = db.scalars(
            select(OutboxMessage)
            .where(OutboxMessage.delivered_at.is_(None))
            .order_by(OutboxMessage.id)
            .limit(limit)
        ).all()
        if not rows:
            return 0
        sent = 0
        for msg in rows:
            self.published.append((msg.routing_key, msg.payload))
            msg.delivered_at = datetime.now(UTC)
            msg.attempts = msg.attempts + 1
            db.commit()
            sent += 1
        return sent

    def by_type(self, message_type: str) -> list[dict]:
        return [env for rk, env in self.published if env["message_type"] == message_type]

    def last(self) -> dict[str, object] | None:
        return self.published[-1][1] if self.published else None


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def settings():
    return Settings.from_environment_or(
        database_url="sqlite://",
    )


@pytest.fixture
def fakes():
    return {"reservation": FakeReservation(), "stripe": FakeStripe()}


@pytest.fixture
def outbox(settings):
    return FakeOutbox(settings)


@pytest.fixture
def service(settings, session_factory, outbox, fakes):
    return CheckoutService(settings, session_factory, outbox, fakes["reservation"], fakes["stripe"])


@pytest.fixture
def app(settings, session_factory, outbox, fakes, service):
    return create_app(
        settings,
        session_factory=session_factory,
        outbox=outbox,
        reservation=fakes["reservation"],
        stripe=fakes["stripe"],
        service=service,
    )


@pytest.fixture
def client(app):
    return app.test_client()


# ----------------------------------------------------------------- helpers


EMAIL = "buyer@example.com"


def buy(client, hold_id: str, user_id: str, *, email: str = EMAIL) -> dict:
    """POST /orders for a buyer and return the parsed body."""
    resp = client.post(
        "/api/v1/orders",
        json={"hold_id": hold_id, "delivery_email": email},
        headers={"X-User-Sub": user_id},
    )
    return json.loads(resp.get_data()) if resp.is_json else {}


def order_by_hold(session_factory, hold_id: str):
    """Fetch an order with its payment + refunds eager-loaded, so the detached object
    can be asserted on after the session closes (no lazy-load on a closed session)."""
    with session_factory() as db:
        return db.scalar(
            select(Order)
            .options(selectinload(Order.payment), selectinload(Order.refunds))
            .where(Order.hold_id == hold_id)
        )


def publish(service, message_type: str, payload: dict, aggregate_id: str) -> None:
    """Stage + deliver one outbox message directly (for consumer-driven tests)."""
    with service._sf() as db:
        service._outbox.write(
            db, message_type=message_type, payload=payload, aggregate_id=aggregate_id
        )
        db.commit()
    service._outbox.deliver_pending(service._sf())


PRODUCERS = {
    "EventCancelled": "event-service",
    "ReservationExpired": "reservation-service",
    "RefundRequired": "reservation-service",
}


def incoming(message_type: str, payload: dict, correlation_id: str = "corr-test") -> Envelope:
    """A message as another service publishes it, run through the real parser - the
    same path the pika consumer takes before dispatching to a handler."""
    body = {
        "message_id": str(uuid.uuid4()),
        "message_type": message_type,
        "schema_version": 1,
        "occurred_at": "2026-11-20T12:00:00Z",
        "producer": PRODUCERS[message_type],
        "payload": payload,
    }
    return parse(json.dumps(body).encode(), correlation_id)
