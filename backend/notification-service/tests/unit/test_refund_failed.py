"""RefundFailed: tell the one buyer their refund didn't go through."""

import json

import pytest

from app.handlers import handle_message
from app.messages import PoisonMessage, RefundFailed, parse
from app.models import KIND_ORDER_REFUNDED, KIND_REFUND_FAILED, STATUS_PENDING
from app.templates import render
from app.user_client import UserNotFound
from tests.payloads import refund_failed_payload
from tests.unit.conftest import (
    EVENT,
    envelope,
    event_published,
    notifications,
    order_refunded,
    refund_failed,
    sub,
    user,
)


def _handle(session_factory, users, events, body: bytes) -> str:
    return handle_message(session_factory, users, events, parse(body))


def test_checkouts_message_parses_without_the_error_text():
    payload = parse(refund_failed(sub(1).upper(), amount=8800)).payload
    assert isinstance(payload, RefundFailed)
    assert payload.user_id == sub(1)
    assert payload.amount == 8800
    assert not hasattr(payload, "last_error")


def test_a_missing_refund_id_is_poison():
    body = refund_failed_payload(sub(1), EVENT)
    del body["refund_id"]
    with pytest.raises(PoisonMessage):
        parse(envelope("RefundFailed", body))


def test_emails_only_the_buyer(session_factory, users, events):
    users.users_by_sub = {sub(1): user(1), sub(2): user(2)}
    _handle(session_factory, users, events, event_published())
    body = refund_failed(sub(1), amount=17600)

    assert _handle(session_factory, users, events, body) == "queued_1"

    rows = notifications(session_factory, kind=KIND_REFUND_FAILED)
    assert [(r.user_id, r.recipient, r.status) for r in rows] == [
        (user(1).user_id, user(1).email, STATUS_PENDING)
    ]
    [row] = rows
    assert row.cognito_sub == sub(1)
    assert row.event_id == EVENT
    assert row.context["amount"] == "SGD 176.00"
    assert row.context["title"] == "Autumn Live"
    assert row.context["order_id"] == parse(body).payload.order_id


def test_the_error_text_is_never_stored(session_factory, users, events):
    users.users_by_sub = {sub(1): user(1)}
    _handle(session_factory, users, events, refund_failed(sub(1)))
    [row] = notifications(session_factory)
    assert "Stripe" not in json.dumps(row.context) and "pi_secret" not in json.dumps(row.context)


def test_a_failure_is_logged_for_whoever_follows_it_up(session_factory, users, events, caplog):
    users.users_by_sub = {sub(1): user(1)}
    body = refund_failed(sub(1))
    payload = parse(body).payload
    _handle(session_factory, users, events, body)
    [record] = [r for r in caplog.records if r.getMessage() == "refund failed"]
    assert record.levelname == "ERROR"
    assert (record.refund_id, record.order_id) == (payload.refund_id, payload.order_id)
    assert "pi_secret" not in record.getMessage() and not hasattr(record, "last_error")


def test_unknown_sub_raises_so_the_message_is_retried(session_factory, users, events):
    with pytest.raises(UserNotFound):
        _handle(session_factory, users, events, refund_failed(sub(9)))
    assert notifications(session_factory) == []


def test_duplicate_message_adds_nothing(session_factory, users, events):
    users.users_by_sub = {sub(1): user(1)}
    body = refund_failed(sub(1))
    _handle(session_factory, users, events, body)
    assert _handle(session_factory, users, events, body) == "duplicate_message"
    assert len(notifications(session_factory)) == 1


def test_same_refund_under_a_new_message_id_adds_nothing(session_factory, users, events):
    users.users_by_sub = {sub(1): user(1)}
    body = refund_failed(sub(1))
    payload = parse(body).payload
    again = refund_failed(sub(1), order_id=payload.order_id, refund_id=payload.refund_id)
    _handle(session_factory, users, events, body)
    assert _handle(session_factory, users, events, again) == "queued_0"
    assert len(notifications(session_factory)) == 1


def test_a_refund_that_later_succeeds_still_gets_its_refund_email(session_factory, users, events):
    users.users_by_sub = {sub(1): user(1)}
    failed = refund_failed(sub(1))
    payload = parse(failed).payload
    _handle(session_factory, users, events, failed)

    later = order_refunded(sub(1), order_id=payload.order_id, refund_id=payload.refund_id)
    assert _handle(session_factory, users, events, later) == "queued_1"

    assert [r.kind for r in notifications(session_factory)] == [
        KIND_REFUND_FAILED,
        KIND_ORDER_REFUNDED,
    ]


def test_the_email_says_the_money_has_not_arrived_and_what_to_do():
    context = {
        "name": "<b>Alice</b>",
        "title": "Autumn <Live>",
        "amount": "SGD 88.00",
        "reason": "event_cancelled",
        "order_id": "o-1",
    }
    subject, text, html = render(KIND_REFUND_FAILED, context)
    assert subject == "We couldn't complete your refund for Autumn <Live>"
    assert "We tried to refund SGD 88.00 for Autumn <Live>" in text
    assert "you haven't received this money yet" in text
    assert "reply to this email" in text
    assert "Order: o-1" in text
    assert "SGD 88.00" in html and "o-1" in html and "reply to this email" in html
    assert "<Live>" not in html and "<b>Alice</b>" not in html
