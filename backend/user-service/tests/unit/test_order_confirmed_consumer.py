"""user-service.OrderConfirmed: record the event in the buyer's purchased events."""

import json

import pytest

from app.messaging.consumer import Disposition, dispose, make_on_message
from app.messaging.envelope import PoisonMessage, parse
from app.messaging.handlers import UnknownUser, handle_message
from tests.unit.conftest import BUYER_SUB, OTHER_SUB, get_user_row
from tests.unit.messaging_helpers import (
    EVENT,
    OTHER_EVENT,
    FakeChannel,
    delivery,
    envelope,
    order_confirmed,
)


def _handle(session_factory, body: bytes) -> str:
    return handle_message(session_factory, parse(body))


def test_adds_the_event_to_the_buyer(session_factory, make_user):
    make_user()
    assert _handle(session_factory, order_confirmed(BUYER_SUB)) == "purchase_recorded"
    assert get_user_row(session_factory).purchased_event_ids == EVENT


def test_matches_the_cognito_sub_case_insensitively(session_factory, make_user):
    make_user()
    _handle(session_factory, order_confirmed(BUYER_SUB.upper()))
    assert get_user_row(session_factory).purchased_event_ids == EVENT


def test_same_event_is_not_added_twice(session_factory, make_user):
    make_user()
    body = order_confirmed(BUYER_SUB)
    _handle(session_factory, body)
    _handle(session_factory, body)  # redelivery of the same message
    # A second order for the same event, under a new message_id.
    assert _handle(session_factory, order_confirmed(BUYER_SUB)) == "already_recorded"
    assert get_user_row(session_factory).purchased_event_ids == EVENT


def test_second_event_is_appended(session_factory, make_user):
    make_user()
    _handle(session_factory, order_confirmed(BUYER_SUB, EVENT))
    _handle(session_factory, order_confirmed(BUYER_SUB, OTHER_EVENT.upper()))
    assert get_user_row(session_factory).purchased_event_ids == f"{EVENT},{OTHER_EVENT}"


def test_only_the_buyer_is_changed(session_factory, make_user):
    make_user()
    make_user(sub=OTHER_SUB, user_id="9f14e45f-ceea-4b6a-9a3b-1f2d3c4e5f61", email="b@example.com")
    _handle(session_factory, order_confirmed(BUYER_SUB))
    assert get_user_row(session_factory, OTHER_SUB).purchased_event_ids is None


def test_unknown_buyer_raises_so_the_message_is_retried(session_factory):
    with pytest.raises(UnknownUser):
        _handle(session_factory, order_confirmed(BUYER_SUB))


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[1, 2]",
        envelope("OrderConfirmed", {"user_id": BUYER_SUB}),  # no event_id
        envelope("OrderConfirmed", {"user_id": BUYER_SUB, "event_id": "nope"}),
        envelope("OrderConfirmed", {"event_id": EVENT}),  # no user_id
        order_confirmed(BUYER_SUB, schema_version=2),
        json.dumps({"message_type": "OrderConfirmed", "payload": {}}).encode(),  # no message_id
    ],
)
def test_poison_messages(body):
    with pytest.raises(PoisonMessage):
        parse(body)


def test_dispositions():
    def ok(_message):
        return "done"

    def broken(_message):
        raise RuntimeError("database down")

    assert dispose(order_confirmed(BUYER_SUB), ok) is Disposition.ACK
    assert dispose(order_confirmed(BUYER_SUB), broken) is Disposition.REQUEUE
    assert dispose(b"not json", ok) is Disposition.DEAD_LETTER
    # A message type this queue doesn't use is acknowledged and ignored.
    assert dispose(envelope("PaymentCompleted", {}), ok) is Disposition.ACK


def test_on_message_acks_rejects_and_never_nacks(session_factory, make_user):
    make_user()
    channel = FakeChannel()
    on_message = make_on_message(lambda m: handle_message(session_factory, m), pause=lambda s: None)

    method, props = delivery(tag=1)
    on_message(channel, method, props, order_confirmed(BUYER_SUB))
    method, props = delivery(tag=2)
    on_message(channel, method, props, order_confirmed(OTHER_SUB))  # unknown user: retry
    method, props = delivery(tag=3)
    on_message(channel, method, props, b"{")  # poison

    assert channel.calls == [("ack", 1), ("reject", 2, True), ("reject", 3, False)]
