"""Parsing the three consumed messages. Tests 1-4 of the plan."""

import json
from pathlib import Path

import pytest

from app.messages import EventCancelled, EventPublished, OrderRefunded, PoisonMessage, parse
from tests.unit.conftest import EVENT, envelope, event_published, order_refunded, sub

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_publishers_example_event_published_parses():
    # Copied from rabbitmq-service contracts/examples: exactly what event-service sends.
    message = parse((FIXTURES / "EventPublished.json").read_bytes())
    assert message.message_type == "EventPublished"
    assert message.message_id == "3d6f0c8e-6a3b-4f7d-9a51-0b2c1e7d9f40"
    p = message.payload
    assert isinstance(p, EventPublished)
    assert p.event_id == EVENT
    assert p.title == "Autumn Live"
    assert p.starts_at.isoformat() == "2026-11-20T12:00:00"
    assert p.release_at.isoformat() == "2026-10-20T12:00:00"
    assert p.queue_join_opens_at.isoformat() == "2026-10-20T11:45:00"


def test_publishers_example_event_cancelled_parses():
    message = parse((FIXTURES / "EventCancelled.json").read_bytes())
    p = message.payload
    assert isinstance(p, EventCancelled)
    assert p.event_id == EVENT
    assert p.reason == "Event cancelled by organiser."


def test_order_refunded_in_checkouts_shape_parses():
    body = order_refunded(sub(1).upper(), amount=17600, reason="event_cancelled")
    p = parse(body, correlation_id="corr-1").payload
    assert isinstance(p, OrderRefunded)
    assert p.user_id == sub(1)  # Cognito subs are compared lower-case
    assert p.event_id == EVENT
    assert p.amount == 17600 and p.currency == "SGD"
    assert p.reason == "event_cancelled"


def test_correlation_id_comes_from_the_amqp_property():
    assert parse(event_published(), correlation_id="abc").correlation_id == "abc"
    assert parse(event_published()).correlation_id is None


def test_unknown_payload_fields_are_ignored():
    raw = json.loads(event_published())
    raw["payload"]["venue"] = "Esplanade"
    assert parse(json.dumps(raw).encode()).payload.title == "Autumn Live"


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        envelope("EventPublished", {"event_id": EVENT}),  # payload missing required fields
        event_published(schema_version=2),
        event_published(schema_version=True),
        event_published(event_id="not-a-uuid"),
        event_published(title=""),
        envelope("EventCancelled", {"event_id": "nope"}),
        order_refunded("", amount=100),
        order_refunded(sub(1), amount=-1),
        order_refunded(sub(1), amount="88.00"),
        json.dumps({"message_type": "EventPublished", "payload": {}}).encode(),
        event_published(unexpected="field"),  # the envelope has exactly six fields
        event_published(occurred_at="yesterday"),
    ],
)
def test_poison_messages(body):
    with pytest.raises(PoisonMessage):
        parse(body)


def test_unknown_message_type_has_no_payload():
    message = parse(envelope("PaymentCompleted", {"order_id": "x"}))
    assert message.message_type == "PaymentCompleted"
    assert message.payload is None
