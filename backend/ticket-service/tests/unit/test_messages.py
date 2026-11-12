import json

import pytest

from app.messages import OrderConfirmed, PoisonMessage, parse_message
from tests.unit.conftest import envelope, new_id, order_payload


def test_parses_the_event_and_queue_envelope():
    payload = order_payload(event_id=new_id().upper())

    message = parse_message(envelope("OrderConfirmed", payload))

    assert isinstance(message.payload, OrderConfirmed)
    assert message.payload.event_id == payload["event_id"].lower()  # canonical UUIDs
    assert message.payload.quantity == 2


def test_accepts_the_technical_contract_envelope_spelling():
    body = {
        "message_id": new_id(),
        "event_type": "OrderConfirmed",
        "schema_version": 1,
        "occurred_at": "2026-11-20T12:06:00Z",
        "correlation_id": "abc-123",
        "data": order_payload(),
    }

    message = parse_message(json.dumps(body).encode())

    assert message.message_type == "OrderConfirmed"
    assert message.envelope.correlation_id == "abc-123"


def test_message_types_ticket_does_not_use_are_ignored():
    assert parse_message(envelope("EventSoldOut", {"event_id": new_id()})) is None


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[1, 2]",
        json.dumps({"message_type": "OrderConfirmed", "payload": {}}).encode(),  # no message_id
        envelope("OrderConfirmed", order_payload(quantity=6)),  # above the five-ticket limit
        envelope("OrderConfirmed", order_payload(quantity=0)),
        envelope("OrderConfirmed", order_payload(order_id="789")),  # not a UUID
        envelope("RefundCompleted", {"order_id": new_id()}),  # event_id missing
        envelope("EventCancelled", {}),
    ],
)
def test_messages_that_can_never_succeed_are_poison(body):
    with pytest.raises(PoisonMessage):
        parse_message(body)


def test_unsupported_schema_version_is_poison():
    body = json.loads(envelope("OrderConfirmed", order_payload()))
    body["schema_version"] = 2

    with pytest.raises(PoisonMessage):
        parse_message(json.dumps(body).encode())


def test_bad_optional_fields_do_not_make_a_message_poison():
    payload = order_payload(delivery_email="not-an-email", confirmed_at="yesterday")

    message = parse_message(envelope("OrderConfirmed", payload))

    assert message.payload.delivery_email is None
    assert message.payload.confirmed_at is None


def test_prototype_email_field_name_is_accepted():
    payload = order_payload()
    payload["customer_email"] = payload.pop("delivery_email")

    message = parse_message(envelope("OrderConfirmed", payload))

    assert message.payload.delivery_email == "buyer@example.com"


@pytest.mark.parametrize("value", [None, "", "not-a-uuid"])
def test_hold_and_admission_ids_are_optional(value):
    # Checkout may confirm an order without an admission_id; Ticket only records them.
    payload = order_payload(hold_id=value, admission_id=value)

    message = parse_message(envelope("OrderConfirmed", payload))

    assert (message.payload.hold_id, message.payload.admission_id) == (None, None)


def test_checkouts_refund_amount_field_is_read():
    payload = {"order_id": new_id(), "event_id": new_id(), "amount": 17600, "currency": "SGD"}

    message = parse_message(envelope("RefundCompleted", payload))

    assert message.payload.refund_amount == 17600
