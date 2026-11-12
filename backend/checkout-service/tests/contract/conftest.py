"""Adapters between rabbitmq-service's generated contract tests and checkout-service's real code.

The service, app, SQLite database and the reservation and Stripe fakes are the shared fixtures
in tests/conftest.py. Only the outbox differs: here it is the real Outbox, so the real relay
(Outbox.deliver_pending) publishes, with only its broker connection replaced.

contract_publish makes checkout publish one message the way production does:
- a buyer orders through the API and Stripe reports the payment (OrderConfirmed)
- the event is then cancelled (the real EventCancelled handler records the refund) and the
  refund processor asks Stripe, which refunds (RefundCompleted, OrderRefunded) or refuses
  (RefundFailed)
- the relay sends every pending row on ticketto_contracts' RecordingChannel, which records the
  publisher confirms and every basic_publish for the contract checks.

contract_queues: the queues the worker consumes (Settings.broker_consumer_queues).
contract_handle: the consumer's real decision for one delivery, `_dispose`, running the real
handler (CheckoutService, on the SQLite database): ack, requeue, or reject.
"""

from __future__ import annotations

import uuid

import pika
import pytest

from app.messaging import outbox as outbox_module
from app.messaging.consumers import QUEUE_TO_HANDLER, _dispose
from app.messaging.handlers import MessageHandlers
from app.messaging.outbox import Outbox
from app.messaging.payloads import QUEUE_MESSAGE_TYPES
from tests.conftest import buy, incoming
from tests.fakes.stripe import payment_succeeded_event

# All ids are UUIDs in the contracts.
BUYER = str(uuid.uuid4())
EVENT = str(uuid.uuid4())

# What happens to an order before checkout publishes each message.
ACTIONS = {
    "OrderConfirmed": ["pay"],
    "RefundCompleted": ["pay", "cancel_event", "refund"],
    "OrderRefunded": ["pay", "cancel_event", "refund"],
    "RefundFailed": ["pay", "cancel_event", "refuse_refund"],
}

# _dispose's (ack, requeue) as the suite's outcomes.
OUTCOMES = {(True, False): "ack", (False, True): "requeue", (False, False): "reject"}


class _Connection:
    """Stands in for pika.BlockingConnection, handing the relay the recording channel."""

    def __init__(self, channel):
        self._channel = channel

    def channel(self):
        return self._channel

    def close(self):
        pass


@pytest.fixture
def outbox(settings):
    """The real outbox (tests/conftest.py's FakeOutbox records rows without publishing)."""
    return Outbox(settings)


def _pay(client, fakes, service) -> None:
    hold = str(uuid.uuid4())
    fakes["reservation"].add_hold(
        hold_id=hold,
        user_id=BUYER,
        event_id=EVENT,
        category_id=str(uuid.uuid4()),
        admission_id=str(uuid.uuid4()),
        quantity=2,
        unit_amount=12_800,
    )
    order = buy(client, hold, BUYER)
    assert order.get("status") == "PENDING_PAYMENT", order
    (intent,) = fakes["stripe"].intents
    service.handle_stripe_event(payment_succeeded_event(intent))


def _cancel_event(service) -> None:
    payload = {
        "event_id": EVENT,
        "event_version": 3,
        "cancellation_id": str(uuid.uuid4()),
        "cancelled_at": "2026-11-20T12:00:00Z",
        "reason": "Venue closure",
    }
    service.handle_event_cancelled(incoming("EventCancelled", payload))


@pytest.fixture
def contract_publish(  # pylint: disable=too-many-arguments,too-many-positional-arguments  # fixtures
    monkeypatch, client, fakes, service, session_factory, outbox
):
    steps = {
        "pay": lambda: _pay(client, fakes, service),
        "cancel_event": lambda: _cancel_event(service),
        "refund": service.process_refunds,
        "refuse_refund": lambda: (
            setattr(fakes["stripe"], "refund_outcome", "failed"),
            service.process_refunds(),
        ),
    }

    def publish(message_name, channel):
        for step in ACTIONS[message_name]:
            steps[step]()
        monkeypatch.setattr(
            outbox_module.pika, "BlockingConnection", lambda _params: _Connection(channel)
        )
        with session_factory() as db:
            outbox.deliver_pending(db)

    return publish


@pytest.fixture
def contract_queues(settings) -> set[str]:
    return set(settings.broker_consumer_queues)


@pytest.fixture
def contract_handle(session_factory, service):
    handlers = MessageHandlers(session_factory, service)

    def handle(queue: str, body: bytes, properties: dict[str, object]) -> str:
        handler = QUEUE_TO_HANDLER[queue]
        ack, requeue = _dispose(
            body,
            pika.BasicProperties(**properties),
            lambda envelope: handler(handlers, envelope),
            QUEUE_MESSAGE_TYPES[queue],
        )
        return OUTCOMES[(ack, requeue)]

    return handle
