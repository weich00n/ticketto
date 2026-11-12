"""user-service.EventCancelled: remember the cancellation; buyers keep the event in their list."""

import pytest
from sqlalchemy import select

from app.messaging.envelope import PoisonMessage, parse
from app.messaging.handlers import handle_message
from app.models import CancelledEvent
from tests.unit.conftest import BUYER_SUB, OTHER_SUB, get_user_row
from tests.unit.messaging_helpers import (
    EVENT,
    OTHER_EVENT,
    envelope,
    event_cancelled,
    order_confirmed,
)


def _handle(session_factory, body: bytes) -> str:
    return handle_message(session_factory, parse(body))


def _cancelled(session_factory) -> list[str]:
    with session_factory() as session:
        return list(session.scalars(select(CancelledEvent.event_id)))


def test_records_the_cancellation(session_factory):
    assert _handle(session_factory, event_cancelled()) == "cancellation_recorded"
    assert _cancelled(session_factory) == [EVENT]


def test_buyers_keep_the_event_so_they_can_be_emailed(client, session_factory, make_user):
    make_user()
    _handle(session_factory, order_confirmed(BUYER_SUB))
    _handle(session_factory, event_cancelled())

    assert get_user_row(session_factory).purchased_event_ids == EVENT
    res = client.get("/internal/v1/users", params={"purchased_event_id": EVENT})
    assert [u["user_id"] for u in res.json()["items"]] == [get_user_row(session_factory).user_id]


def test_late_order_for_a_cancelled_event_is_not_added(session_factory, make_user):
    make_user()
    _handle(session_factory, event_cancelled())
    # Acked (returns normally), not retried: the purchase is void.
    assert _handle(session_factory, order_confirmed(BUYER_SUB)) == "event_cancelled"
    assert get_user_row(session_factory).purchased_event_ids is None


def test_late_order_for_a_cancelled_event_with_unknown_buyer_is_still_acked(session_factory):
    _handle(session_factory, event_cancelled())
    assert _handle(session_factory, order_confirmed(OTHER_SUB)) == "event_cancelled"


def test_order_for_another_live_event_is_still_added(session_factory, make_user):
    make_user()
    _handle(session_factory, event_cancelled(EVENT))
    _handle(session_factory, order_confirmed(BUYER_SUB, OTHER_EVENT))
    assert get_user_row(session_factory).purchased_event_ids == OTHER_EVENT


def test_duplicate_cancellation_is_a_no_op(session_factory):
    _handle(session_factory, event_cancelled())
    assert _handle(session_factory, event_cancelled()) == "already_cancelled"
    assert _cancelled(session_factory) == [EVENT]


@pytest.mark.parametrize(
    "body",
    [
        envelope("EventCancelled", {}),
        envelope("EventCancelled", {"event_id": "not-a-uuid"}),
        event_cancelled(schema_version=3),
    ],
)
def test_poison_cancellations(body):
    with pytest.raises(PoisonMessage):
        parse(body)
