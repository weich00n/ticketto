"""EventPublished: queue one new-event email per opted-in user. Tests 5-10 of the plan."""

from datetime import datetime

import pytest

from app.handlers import handle_message
from app.messages import parse
from app.models import KIND_EVENT_PUBLISHED, STATUS_PENDING, Event, ProcessedMessage
from app.user_client import UserServiceUnavailable
from tests.unit.conftest import (
    EVENT,
    event_cancelled,
    event_published,
    notifications,
    sub,
    user,
)


def _handle(session_factory, users, events, body: bytes) -> str:
    return handle_message(session_factory, users, events, parse(body))


def test_queues_one_email_per_opted_in_user(session_factory, users, events):
    users.opted_in_users = [user(1), user(2), user(3)]

    outcome = _handle(session_factory, users, events, event_published())

    assert outcome == "queued_3"
    rows = notifications(session_factory)
    assert [(r.kind, r.user_id, r.recipient, r.status) for r in rows] == [
        (KIND_EVENT_PUBLISHED, user(n).user_id, user(n).email, STATUS_PENDING) for n in (1, 2, 3)
    ]
    assert rows[0].event_id == EVENT
    assert rows[0].context["title"] == "Autumn Live"
    assert rows[0].context["name"] == "User 1"
    assert rows[0].context["starts_at"] == "2026-11-20T12:00:00Z"
    assert rows[0].context["release_at"] == "2026-10-20T12:00:00Z"
    assert rows[0].context["queue_join_opens_at"] == "2026-10-20T11:45:00Z"
    assert rows[0].next_attempt_at is not None  # due now


def test_saves_the_event_details(session_factory, users, events):
    _handle(session_factory, users, events, event_published())
    with session_factory() as session:
        event = session.get(Event, EVENT)
    assert event.title == "Autumn Live"
    assert event.starts_at == datetime(2026, 11, 20, 12, 0)
    assert event.announced_at is not None and event.cancelled_at is None


def test_users_who_opted_out_are_not_listed_so_not_emailed(session_factory, users, events):
    # user-service filters on notify_new_events; the handler emails exactly who it returns.
    users.opted_in_users = [user(2)]
    _handle(session_factory, users, events, event_published())
    assert [r.user_id for r in notifications(session_factory)] == [user(2).user_id]
    assert users.calls == [("opted_in",)]


def test_same_message_twice_adds_nothing(session_factory, users, events):
    users.opted_in_users = [user(1)]
    body = event_published()
    _handle(session_factory, users, events, body)
    assert _handle(session_factory, users, events, body) == "duplicate_message"
    assert len(notifications(session_factory)) == 1
    assert users.calls == [("opted_in",)]  # a duplicate doesn't even ask user-service


def test_new_message_id_for_the_same_event_adds_nothing(session_factory, users, events):
    users.opted_in_users = [user(1), user(2)]
    _handle(session_factory, users, events, event_published())
    assert _handle(session_factory, users, events, event_published()) == "already_announced"
    assert len(notifications(session_factory)) == 2


def test_already_cancelled_event_is_not_announced(session_factory, users, events):
    users.opted_in_users = [user(1)]
    _handle(session_factory, users, events, event_cancelled())  # arrived first
    assert _handle(session_factory, users, events, event_published()) == "event_cancelled"
    assert notifications(session_factory, kind=KIND_EVENT_PUBLISHED) == []


def test_user_service_down_raises_and_writes_nothing(session_factory, users, events):
    users.down = True
    with pytest.raises(UserServiceUnavailable):
        _handle(session_factory, users, events, event_published())
    assert notifications(session_factory) == []
    with session_factory() as session:
        assert session.query(ProcessedMessage).count() == 0
        assert session.get(Event, EVENT) is None


def test_no_opted_in_users_is_processed(session_factory, users, events):
    assert _handle(session_factory, users, events, event_published()) == "queued_0"
    with session_factory() as session:
        assert session.get(Event, EVENT).announced_at is not None


def test_each_email_stores_the_users_cognito_sub(session_factory, users, events):
    # The notifications API finds a signed-in user's rows by the sub in their token.
    users.opted_in_users = [user(1), user(2)]
    _handle(session_factory, users, events, event_published())
    assert [r.cognito_sub for r in notifications(session_factory)] == [sub(1), sub(2)]
