# pylint: disable=unbalanced-tuple-unpacking  # [x] = ... asserts exactly one item
"""TicketIssued: email the buyer their tickets and list the order in their notifications.
Tests 1-9 of rollout-plan.md, Part 2."""

import json
from pathlib import Path

import pytest
from sqlalchemy import update

from app.handlers import handle_message
from app.messages import PoisonMessage, TicketIssued, parse
from app.models import (
    KIND_TICKET_ISSUED,
    STATUS_CANCELLED,
    STATUS_PENDING,
    STATUS_SENT,
    Notification,
)
from app.templates import render
from app.user_client import UserNotFound, UserServiceUnavailable
from tests.payloads import ticket_issued_payload
from tests.unit.conftest import (
    EVENT,
    OTHER_EVENT,
    envelope,
    event_cancelled,
    event_published,
    notifications,
    order_refunded,
    refund_failed,
    sub,
    ticket_context,
    ticket_issued,
    user,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _handle(session_factory, users, events, body: bytes) -> str:
    return handle_message(session_factory, users, events, parse(body))


def _qr_codes(row) -> list:
    return [t.get("qr_payload") for t in row.context["tickets"]]


# ---------- parsing ----------


def test_the_publishers_sample_parses():
    message = parse((FIXTURES / "TicketIssued.json").read_bytes())
    assert isinstance(message.payload, TicketIssued)
    assert message.payload.title == "Autumn Live"
    assert [t.ticket_number for t in message.payload.tickets] == [1, 2]
    assert message.payload.tickets[0].qr_payload.startswith("TKT1.k1.")


@pytest.mark.parametrize(
    "field", ["delivery_email", "title", "venue", "starts_at", "category_name"]
)
def test_fields_ticket_service_may_not_know_can_be_null(field):
    payload = {**ticket_issued_payload(sub(1), EVENT), field: None}
    assert getattr(parse(envelope("TicketIssued", payload)).payload, field) is None


def _without_qr_code(payload: dict) -> dict:
    ticket = {k: v for k, v in payload["tickets"][0].items() if k != "qr_payload"}
    return {**payload, "tickets": [ticket]}


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param(lambda p: {k: v for k, v in p.items() if k != "tickets"}, id="no tickets"),
        pytest.param(lambda p: {**p, "tickets": []}, id="empty tickets"),
        pytest.param(lambda p: {**p, "tickets": p["tickets"][:1] * 6}, id="more than 5"),
        pytest.param(_without_qr_code, id="no qr_payload"),
        pytest.param(lambda p: {**p, "order_id": "not-a-uuid"}, id="bad order_id"),
        pytest.param(lambda p: {**p, "starts_at": "tomorrow"}, id="bad starts_at"),
    ],
)
def test_a_message_that_can_never_work_is_poison(broken):
    with pytest.raises(PoisonMessage):
        parse(envelope("TicketIssued", broken(ticket_issued_payload(sub(1), EVENT))))


# ---------- who gets it ----------


def test_one_email_to_delivery_email_with_the_details(session_factory, users, events):
    body = ticket_issued(sub(1))
    order_id = parse(body).payload.order_id

    assert _handle(session_factory, users, events, body) == "queued_1"

    [row] = notifications(session_factory, kind=KIND_TICKET_ISSUED)
    assert (row.recipient, row.status) == ("buyer@example.com", STATUS_PENDING)
    assert row.event_id == EVENT
    assert row.context["order_id"] == order_id
    subject, text, html = render(row.kind, row.context)
    assert subject == "Your 2 tickets for Autumn Live"
    for part in (text, html):
        assert "Autumn Live" in part
        assert "National Stadium" in part
        assert "20 Nov 2026 · 20:00 SGT" in part
        assert "CAT 1" in part
        assert "1 of 2" in part and "2 of 2" in part


def test_one_ticket_reads_as_one(session_factory, users, events):
    payload = ticket_issued_payload(sub(1), EVENT, tickets=1)
    _handle(session_factory, users, events, envelope("TicketIssued", payload))
    [row] = notifications(session_factory)
    subject, text, _html = render(row.kind, row.context)
    assert subject == "Your 1 ticket for Autumn Live"
    assert "Your ticket for Autumn Live is confirmed." in text


def test_the_ticket_shows_the_buyers_name_but_goes_to_delivery_email(
    session_factory, users, events
):
    users.users_by_sub = {sub(1): user(1)}
    _handle(session_factory, users, events, ticket_issued(sub(1)))
    [row] = notifications(session_factory)
    assert row.recipient == "buyer@example.com"
    assert row.context["name"] == user(1).name
    assert row.cognito_sub == sub(1)


def test_a_user_service_outage_cant_hold_up_tickets(session_factory, users, events):
    users.down = True  # the name is a nicety: the email only needs delivery_email
    assert _handle(session_factory, users, events, ticket_issued(sub(1))) == "queued_1"
    [row] = notifications(session_factory)
    assert (row.recipient, row.context["name"]) == ("buyer@example.com", "")


def test_a_buyer_without_a_profile_still_gets_their_tickets(session_factory, users, events):
    assert _handle(session_factory, users, events, ticket_issued(sub(9))) == "queued_1"
    [row] = notifications(session_factory)
    assert (row.recipient, row.cognito_sub) == ("buyer@example.com", sub(9))


def test_with_no_address_at_all_an_outage_is_retried(session_factory, users, events):
    users.down = True
    with pytest.raises(UserServiceUnavailable):
        _handle(session_factory, users, events, ticket_issued(sub(1), delivery_email=None))
    assert notifications(session_factory) == []


def test_without_delivery_email_the_buyer_is_looked_up_by_sub(session_factory, users, events):
    users.users_by_sub = {sub(1): user(1)}

    body = ticket_issued(sub(1), delivery_email=None)
    assert _handle(session_factory, users, events, body) == "queued_1"

    [row] = notifications(session_factory)
    assert (row.recipient, row.context["name"]) == (user(1).email, user(1).name)
    assert row.cognito_sub == sub(1)


def test_without_delivery_email_an_unknown_sub_is_retried(session_factory, users, events):
    with pytest.raises(UserNotFound):
        _handle(session_factory, users, events, ticket_issued(sub(9), delivery_email=None))
    assert notifications(session_factory) == []


def test_opted_out_users_still_get_their_tickets(session_factory, users, events):
    users.opted_in_users = []  # nobody wants new-event emails: this isn't one
    assert _handle(session_factory, users, events, ticket_issued(sub(1))) == "queued_1"
    assert ("opted_in",) not in users.calls


def test_the_row_stores_the_sub_for_the_notifications_api(session_factory, users, events):
    _handle(session_factory, users, events, ticket_issued(sub(1).upper()))
    [row] = notifications(session_factory)
    assert row.cognito_sub == sub(1)


# ---------- duplicates ----------


def test_the_same_message_again_adds_nothing(session_factory, users, events):
    body = ticket_issued(sub(1))
    _handle(session_factory, users, events, body)
    assert _handle(session_factory, users, events, body) == "duplicate_message"
    assert len(notifications(session_factory)) == 1


def test_a_republish_under_a_new_message_id_sends_it_again(session_factory, users, events):
    # ticket-service's support endpoint: POST /internal/v1/orders/{id}/ticket-issued/republish
    payload = ticket_issued_payload(sub(1), EVENT)
    _handle(session_factory, users, events, envelope("TicketIssued", payload))

    resend = envelope("TicketIssued", payload)
    assert _handle(session_factory, users, events, resend) == "queued_1"

    assert len(notifications(session_factory)) == 2


# ---------- what the email is called ----------


def test_a_missing_title_comes_from_the_events_row(session_factory, users, events):
    users.opted_in_users = []
    _handle(session_factory, users, events, event_published(title="From the event"))

    _handle(session_factory, users, events, ticket_issued(sub(1), title=None, starts_at=None))

    [row] = notifications(session_factory, kind=KIND_TICKET_ISSUED)
    assert row.context["title"] == "From the event"
    assert row.context["starts_at"] == "2026-11-20T12:00:00Z"
    assert events.calls == []


def test_a_missing_title_then_comes_from_event_service(session_factory, users, events):
    events.details_by_event = {EVENT: {"title": "From event-service", "starts_at": None}}
    _handle(session_factory, users, events, ticket_issued(sub(1), title=None))
    [row] = notifications(session_factory)
    assert row.context["title"] == "From event-service"
    assert events.calls == [EVENT]


def test_a_missing_title_finally_says_your_event(session_factory, users, events):
    _handle(session_factory, users, events, ticket_issued(sub(1), title=None))
    [row] = notifications(session_factory)
    assert row.context["title"] == "your event"
    assert render(row.kind, row.context)[0] == "Your 2 tickets for your event"


def test_the_title_in_the_message_needs_no_lookup(session_factory, users, events):
    _handle(session_factory, users, events, ticket_issued(sub(1)))
    assert events.calls == []


def test_the_message_keeps_the_title_for_later_emails(session_factory, users, events):
    users.users_by_sub = {sub(1): user(1)}
    _handle(session_factory, users, events, ticket_issued(sub(1)))
    _handle(session_factory, users, events, refund_failed(sub(1)))
    [row] = notifications(session_factory, kind="REFUND_FAILED")
    assert row.context["title"] == "Autumn Live"


# ---------- tickets that are no longer any use ----------


def test_nothing_is_queued_after_the_events_cancellation(session_factory, users, events):
    _handle(session_factory, users, events, event_cancelled())
    assert _handle(session_factory, users, events, ticket_issued(sub(1))) == "event_cancelled"
    assert notifications(session_factory, kind=KIND_TICKET_ISSUED) == []


def test_nothing_is_queued_after_the_orders_refund(session_factory, users, events):
    users.users_by_sub = {sub(1): user(1)}
    payload = ticket_issued_payload(sub(1), EVENT)
    _handle(session_factory, users, events, order_refunded(sub(1), order_id=payload["order_id"]))

    body = envelope("TicketIssued", payload)
    assert _handle(session_factory, users, events, body) == "order_refunded"
    assert notifications(session_factory, kind=KIND_TICKET_ISSUED) == []


def test_another_orders_refund_doesnt_stop_it(session_factory, users, events):
    users.users_by_sub = {sub(1): user(1)}
    _handle(session_factory, users, events, order_refunded(sub(1)))
    assert _handle(session_factory, users, events, ticket_issued(sub(1))) == "queued_1"


def test_a_failed_refund_doesnt_stop_it(session_factory, users, events):
    # The buyer hasn't had their money back, and their tickets are still valid.
    users.users_by_sub = {sub(1): user(1)}
    payload = ticket_issued_payload(sub(1), EVENT)
    _handle(session_factory, users, events, refund_failed(sub(1), order_id=payload["order_id"]))
    assert _handle(session_factory, users, events, envelope("TicketIssued", payload)) == "queued_1"


def test_a_cancellation_cancels_the_events_pending_ticket_emails(session_factory, users, events):
    _handle(session_factory, users, events, ticket_issued(sub(1)))
    _handle(session_factory, users, events, ticket_issued(sub(2), OTHER_EVENT))

    _handle(session_factory, users, events, event_cancelled())

    [cancelled] = notifications(session_factory, kind=KIND_TICKET_ISSUED, event_id=EVENT)
    assert (cancelled.status, cancelled.next_attempt_at) == (STATUS_CANCELLED, None)
    assert _qr_codes(cancelled) == [None, None]
    [other] = notifications(session_factory, kind=KIND_TICKET_ISSUED, event_id=OTHER_EVENT)
    assert other.status == STATUS_PENDING and all(_qr_codes(other))


def test_a_refund_cancels_only_that_orders_pending_ticket_email(session_factory, users, events):
    users.users_by_sub = {sub(1): user(1)}
    payload = ticket_issued_payload(sub(1), EVENT)
    _handle(session_factory, users, events, envelope("TicketIssued", payload))
    _handle(session_factory, users, events, ticket_issued(sub(2)))

    _handle(session_factory, users, events, order_refunded(sub(1), order_id=payload["order_id"]))

    rows = {
        r.context["order_id"]: r for r in notifications(session_factory, kind=KIND_TICKET_ISSUED)
    }
    refunded = rows.pop(payload["order_id"])
    assert refunded.status == STATUS_CANCELLED and _qr_codes(refunded) == [None, None]
    [other] = rows.values()
    assert other.status == STATUS_PENDING and all(_qr_codes(other))


def test_a_ticket_email_already_sent_is_left_alone(session_factory, users, events):
    _handle(session_factory, users, events, ticket_issued(sub(1)))
    with session_factory() as session, session.begin():
        session.execute(update(Notification).values(status=STATUS_SENT, next_attempt_at=None))

    _handle(session_factory, users, events, event_cancelled())

    [row] = notifications(session_factory, kind=KIND_TICKET_ISSUED)
    assert row.status == STATUS_SENT


def test_the_qr_codes_wait_in_the_row_only_for_sending(session_factory, users, events):
    body = ticket_issued(sub(1))
    _handle(session_factory, users, events, body)
    [row] = notifications(session_factory)
    expected = [t.qr_payload for t in parse(body).payload.tickets]
    assert _qr_codes(row) == expected
    assert json.dumps(row.context).count("TKT1.") == 2


# ---------- the email looks like the ticket on the site (frontend ticket-view.tsx) ----------


def _html(context: dict) -> str:
    return render(KIND_TICKET_ISSUED, context)[2]


def test_each_ticket_gets_its_own_card_with_the_sites_wording():
    html = _html({**ticket_context(), "name": "Alice"})
    assert html.count("TICKETTO · YOUR INVITATION") == 2
    assert html.count("An evening to remember awaits.") == 2
    assert html.count("Arrive 45 minutes early.") == 2
    for label in ("GUEST", "CATEGORY", "SEAT"):
        assert html.count(label) == 2
    assert html.count("Alice") >= 2 and html.count("CAT 1") == 2
    assert "1 of 2" in html and "2 of 2" in html
    for ticket in ticket_context()["tickets"]:
        assert ticket["ticket_id"] in html


def test_without_a_name_the_guest_line_is_left_out():
    assert "GUEST" not in _html({**ticket_context(), "name": ""})


def test_without_a_category_it_says_general_admission_like_the_site():
    assert "General admission" in _html({**ticket_context(), "category_name": None})


def test_unknown_details_are_left_out_rather_than_guessed():
    html = _html({**ticket_context(), "starts_at": None, "venue": None})
    assert "SGT" not in html and "None" not in html


def test_seat_never_reads_higher_than_its_total():
    # A republish only carries the tickets still valid: here, the second of two.
    context = ticket_context()
    context["tickets"] = context["tickets"][1:]
    assert "2 of 2" in _html(context)


def test_it_uses_only_what_email_clients_support():
    html = _html({**ticket_context(), "name": "Alice"})
    assert "<style" not in html and "class=" not in html and "<svg" not in html
    assert "display:flex" not in html and "display:grid" not in html


def test_every_value_in_the_html_is_escaped():
    bad = '<b>&"bad'
    context = {
        **ticket_context(),
        "title": bad,
        "venue": bad,
        "category_name": bad,
        "name": bad,
        "order_id": bad,
    }
    html = _html(context)
    assert "<b>" not in html
    assert html.count("&lt;b&gt;&amp;&quot;bad") >= 5
