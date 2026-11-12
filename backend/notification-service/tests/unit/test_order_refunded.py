"""OrderRefunded: email the one buyer. Tests 16-19 of the plan."""

from dataclasses import replace

import pytest

from app.handlers import handle_message
from app.messages import parse
from app.models import KIND_ORDER_REFUNDED, STATUS_PENDING, Event
from app.user_client import UserNotFound
from tests.unit.conftest import EVENT, event_published, notifications, order_refunded, sub, user


def _handle(session_factory, users, events, body: bytes) -> str:
    return handle_message(session_factory, users, events, parse(body))


def test_emails_the_user_found_by_sub(session_factory, users, events):
    users.users_by_sub = {sub(1): user(1)}
    _handle(session_factory, users, events, event_published())
    body = order_refunded(sub(1), amount=17600, reason="event_cancelled")
    order_id = parse(body).payload.order_id

    assert _handle(session_factory, users, events, body) == "queued_1"

    [row] = notifications(session_factory)
    assert (row.kind, row.user_id, row.recipient, row.status) == (
        KIND_ORDER_REFUNDED,
        user(1).user_id,
        user(1).email,
        STATUS_PENDING,
    )
    assert row.event_id == EVENT
    assert row.context["amount"] == "SGD 176.00"
    assert row.context["reason"] == "event_cancelled"
    assert row.context["order_id"] == order_id
    assert row.context["title"] == "Autumn Live"
    assert users.calls[-1] == ("by_sub", sub(1))  # after the opted_in call for the publish


def test_unknown_sub_raises_so_the_message_is_retried(session_factory, users, events):
    with pytest.raises(UserNotFound):
        _handle(session_factory, users, events, order_refunded(sub(9)))
    assert notifications(session_factory) == []


def test_duplicate_message_adds_nothing(session_factory, users, events):
    users.users_by_sub = {sub(1): user(1)}
    body = order_refunded(sub(1))
    _handle(session_factory, users, events, body)
    assert _handle(session_factory, users, events, body) == "duplicate_message"
    assert len(notifications(session_factory)) == 1


def test_same_refund_under_a_new_message_id_adds_nothing(session_factory, users, events):
    users.users_by_sub = {sub(1): user(1)}
    body = order_refunded(sub(1))
    _handle(session_factory, users, events, body)
    payload = parse(body).payload
    again = order_refunded(sub(1), order_id=payload.order_id, refund_id=payload.refund_id)
    assert _handle(session_factory, users, events, again) == "queued_0"
    assert len(notifications(session_factory)) == 1


def test_two_refunds_for_the_same_event_are_two_emails(session_factory, users, events):
    users.users_by_sub = {sub(1): user(1)}
    _handle(session_factory, users, events, order_refunded(sub(1)))
    _handle(session_factory, users, events, order_refunded(sub(1)))  # another order
    assert len(notifications(session_factory)) == 2


def test_title_comes_from_the_saved_event(session_factory, users, events):
    users.users_by_sub = {sub(1): user(1)}
    _handle(session_factory, users, events, event_published(title="Autumn Live"))
    _handle(session_factory, users, events, order_refunded(sub(1)))
    assert notifications(session_factory)[-1].context["title"] == "Autumn Live"
    assert events.calls == []


def test_title_fetched_from_event_service_and_saved(session_factory, users, events):
    users.users_by_sub = {sub(1): user(1)}
    events.details_by_event = {EVENT: {"title": "Winter Gala", "starts_at": None}}
    _handle(session_factory, users, events, order_refunded(sub(1)))
    assert notifications(session_factory)[0].context["title"] == "Winter Gala"
    with session_factory() as session:
        assert session.get(Event, EVENT).title == "Winter Gala"
    _handle(session_factory, users, events, order_refunded(sub(1)))
    assert events.calls == [EVENT]  # asked once, then read from the saved row


def test_title_falls_back_to_your_event(session_factory, users, events):
    users.users_by_sub = {sub(1): user(1)}
    _handle(session_factory, users, events, order_refunded(sub(1)))  # event-service knows nothing
    assert notifications(session_factory)[0].context["title"] == "your event"


def test_refund_email_stores_the_sub_from_the_message(session_factory, users, events):
    # user-service's by-sub answer has no cognito_sub; the message's user_id is the sub.
    users.users_by_sub = {sub(1): replace(user(1), cognito_sub=None)}
    _handle(session_factory, users, events, order_refunded(sub(1).upper()))
    [row] = notifications(session_factory)
    assert row.cognito_sub == sub(1)
