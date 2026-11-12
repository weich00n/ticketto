"""EventCancelled: email the event's buyers, and drop new-event emails not yet sent.
Tests 11-15 of the plan."""

import pytest
from sqlalchemy import update

from app.handlers import handle_message
from app.messages import parse
from app.models import (
    KIND_EVENT_CANCELLED,
    KIND_EVENT_PUBLISHED,
    STATUS_CANCELLED,
    STATUS_PENDING,
    STATUS_SENT,
    Event,
    Notification,
)
from app.user_client import UserServiceUnavailable
from tests.unit.conftest import (
    EVENT,
    OTHER_EVENT,
    event_cancelled,
    event_published,
    notifications,
    sub,
    user,
)


def _handle(session_factory, users, events, body: bytes) -> str:
    return handle_message(session_factory, users, events, parse(body))


def test_only_buyers_get_a_cancellation_email(session_factory, users, events):
    users.buyers_by_event = {EVENT: [user(1), user(3)], OTHER_EVENT: [user(2)]}
    _handle(session_factory, users, events, event_published())  # caches the title

    assert _handle(session_factory, users, events, event_cancelled()) == "queued_2"

    rows = notifications(session_factory, kind=KIND_EVENT_CANCELLED)
    assert [(r.user_id, r.recipient, r.status) for r in rows] == [
        (user(1).user_id, user(1).email, STATUS_PENDING),
        (user(3).user_id, user(3).email, STATUS_PENDING),
    ]
    assert rows[0].context["reason"] == "Venue unavailable."
    assert rows[0].context["title"] == "Autumn Live"
    assert users.calls[-1] == ("buyers", EVENT)


def test_records_the_cancellation(session_factory, users, events):
    _handle(session_factory, users, events, event_cancelled())
    with session_factory() as session:
        assert session.get(Event, EVENT).cancelled_at is not None


def test_pending_new_event_emails_are_cancelled_but_sent_ones_kept(session_factory, users, events):
    users.opted_in_users = [user(1), user(2)]
    _handle(session_factory, users, events, event_published())
    with session_factory() as session, session.begin():
        session.execute(
            update(Notification)
            .where(Notification.user_id == user(1).user_id)
            .values(status=STATUS_SENT)
        )

    _handle(session_factory, users, events, event_cancelled())

    statuses = {
        r.user_id: r.status for r in notifications(session_factory, kind=KIND_EVENT_PUBLISHED)
    }
    assert statuses == {user(1).user_id: STATUS_SENT, user(2).user_id: STATUS_CANCELLED}


def test_other_events_new_event_emails_are_untouched(session_factory, users, events):
    users.opted_in_users = [user(1)]
    _handle(session_factory, users, events, event_published(OTHER_EVENT))
    _handle(session_factory, users, events, event_cancelled(EVENT))
    assert [r.status for r in notifications(session_factory)] == [STATUS_PENDING]


def test_duplicate_cancellation_adds_nothing(session_factory, users, events):
    users.buyers_by_event = {EVENT: [user(1)]}
    body = event_cancelled()
    _handle(session_factory, users, events, body)
    assert _handle(session_factory, users, events, body) == "duplicate_message"
    # A second EventCancelled for the same event under a new message_id: no second email.
    assert _handle(session_factory, users, events, event_cancelled()) == "queued_0"
    assert len(notifications(session_factory)) == 1


def test_event_without_buyers_is_processed(session_factory, users, events):
    assert _handle(session_factory, users, events, event_cancelled()) == "queued_0"
    assert notifications(session_factory) == []


def test_title_fetched_from_event_service_when_not_cached(session_factory, users, events):
    users.buyers_by_event = {EVENT: [user(1)]}
    events.details_by_event = {EVENT: {"title": "Winter Gala", "starts_at": None}}
    _handle(session_factory, users, events, event_cancelled())
    assert notifications(session_factory)[0].context["title"] == "Winter Gala"


def test_user_service_down_raises_and_writes_nothing(session_factory, users, events):
    users.opted_in_users = [user(1)]
    _handle(session_factory, users, events, event_published())
    users.down = True
    with pytest.raises(UserServiceUnavailable):
        _handle(session_factory, users, events, event_cancelled())
    with session_factory() as session:
        assert session.get(Event, EVENT).cancelled_at is None
    assert [r.status for r in notifications(session_factory)] == [STATUS_PENDING]


def test_each_cancellation_email_stores_the_buyers_cognito_sub(session_factory, users, events):
    users.buyers_by_event = {EVENT: [user(1), user(2)]}
    _handle(session_factory, users, events, event_cancelled())
    rows = notifications(session_factory, kind=KIND_EVENT_CANCELLED)
    assert [r.cognito_sub for r in rows] == [sub(1), sub(2)]
