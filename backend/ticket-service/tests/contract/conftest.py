"""Adapters between rabbitmq-service's generated contract tests and ticket-service's real code.

The settings, app, SQLite database and QR signer are the unit tests' (tests/unit/conftest.py).

contract_publish makes Ticket publish TicketIssued the way production does: the real handler
(issuance.handle_message) applies an EventPublished and an OrderConfirmed, which issues the
tickets and writes the outbox row; then the real relay (outbox.run_once with RabbitPublisher)
publishes it. Only the broker connection is replaced: its channel is ticketto_contracts'
RecordingChannel, which records the exchange declare, publisher confirms and basic_publish.

contract_queues: the queues the worker consumes (consumer.CONSUMER_QUEUES).
contract_handle: the consumer's real decision for one delivery, consumer.dispose, running the
real handler on the SQLite database: ack, requeue, or dead-letter (reported as "reject").
"""

from __future__ import annotations

import uuid

import pytest

from app import outbox
from app.consumer import CONSUMER_QUEUES, Disposition, dispose
from app.issuance import handle_message
from app.outbox import RabbitPublisher
from tests.unit import conftest as unit

# The unit tests' fixtures, re-exported so pytest finds them here.
settings = unit.settings
app = unit.app
session_factory = unit.session_factory
signer = unit.signer
deliver = unit.deliver

OUTCOMES = {
    Disposition.ACK: "ack",
    Disposition.REQUEUE: "requeue",
    Disposition.DEAD_LETTER: "reject",
}


class _Connection:
    """Stands in for pika.BlockingConnection, handing the publisher the recording channel."""

    def __init__(self, channel):
        self._channel = channel
        self.is_open = True

    def channel(self):
        return self._channel

    def process_data_events(self, time_limit=0):
        pass

    def close(self):
        self.is_open = False


def _event_published(event_id: str, category_id: str) -> dict:
    return {
        "event_id": event_id,
        "event_version": 2,
        "title": "The Midnight Symphony",
        "venue": "Esplanade Concert Hall",
        "starts_at": "2026-12-20T12:00:00Z",
        "release_at": "2026-12-01T02:00:00Z",
        "queue_join_opens_at": "2026-12-01T01:30:00Z",
        "published_at": "2026-11-20T12:00:00Z",
        "total_capacity": 300,
        "ticket_categories": [
            {"category_id": category_id, "name": "CAT 1", "price_amount": 8800, "currency": "SGD"}
        ],
    }


@pytest.fixture()
def contract_publish(monkeypatch, deliver, session_factory, settings):
    def publish(message_name, channel):
        assert message_name == "TicketIssued", message_name
        event_id, category_id = str(uuid.uuid4()), str(uuid.uuid4())
        deliver("EventPublished", _event_published(event_id, category_id))
        deliver(
            "OrderConfirmed",
            unit.order_payload(
                event_id=event_id, category_id=category_id, total_amount=17600, currency="SGD"
            ),
        )
        monkeypatch.setattr(outbox.pika, "BlockingConnection", lambda _params: _Connection(channel))
        publisher = RabbitPublisher(settings.rabbitmq_url)
        try:
            assert outbox.run_once(session_factory, settings, publisher) == 1
        finally:
            publisher.close()

    return publish


@pytest.fixture()
def contract_queues() -> set[str]:
    return {queue.name for queue in CONSUMER_QUEUES}


@pytest.fixture()
def contract_handle(session_factory, signer):
    def handle(_queue: str, body: bytes, properties: dict[str, object]) -> str:
        disposition = dispose(
            body,
            lambda message: handle_message(session_factory, signer, message),
            amqp_correlation_id=properties.get("correlation_id"),
        )
        return OUTCOMES[disposition]

    return handle
