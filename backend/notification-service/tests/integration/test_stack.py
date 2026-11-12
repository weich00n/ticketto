"""End to end: messages on the real broker become emails in Mailpit. Tests 36-40 of the plan."""

import time
import uuid

import pytest

from tests.integration.conftest import emails_to, inline_files, wait_until
from tests.payloads import event_published_payload, refund_failed_payload, ticket_issued_payload

pytestmark = pytest.mark.integration


def _subjects(address: str) -> list[str]:
    return [m["Subject"] for m in emails_to(address)]


def _settle():
    """Long enough for the runner to have sent anything else it was going to send."""
    time.sleep(8)


def test_new_event_reaches_only_opted_in_users(broker, people):
    keen, opted_out = people.add(notify=True), people.add(notify=False)
    title = f"IT Gala {uuid.uuid4().hex[:6]}"

    broker.publish("EventPublished", event_published_payload(str(uuid.uuid4()), title))

    wait_until(lambda: _subjects(keen["email"]), "the new-event email")
    assert _subjects(keen["email"]) == [f"New on Ticketto: {title}"]
    _settle()
    assert _subjects(opted_out["email"]) == []


def test_cancellation_reaches_only_buyers(broker, people):
    event_id = str(uuid.uuid4())
    buyer = people.add(notify=False, bought=(event_id,))  # opted out of new-event emails
    other = people.add(notify=True, bought=(str(uuid.uuid4()),))
    title = f"IT Fest {uuid.uuid4().hex[:6]}"
    broker.publish("EventPublished", event_published_payload(event_id, title))
    wait_until(lambda: _subjects(other["email"]), "the new-event email to the other user")

    broker.publish(
        "EventCancelled",
        {
            "event_id": event_id,
            "event_version": 3,
            "cancellation_id": str(uuid.uuid4()),
            "cancelled_at": "2026-10-25T10:00:00Z",
            "reason": "Integration test.",
        },
    )

    wait_until(lambda: _subjects(buyer["email"]), "the cancellation email")
    assert _subjects(buyer["email"]) == [f"Cancelled: {title}"]
    _settle()
    assert _subjects(other["email"]) == [f"New on Ticketto: {title}"]  # no cancellation


def test_refund_reaches_only_that_buyer(broker, people):
    event_id = str(uuid.uuid4())
    buyer, other = people.add(bought=(event_id,)), people.add(bought=(event_id,))

    broker.publish(
        "OrderRefunded",
        {
            "order_id": str(uuid.uuid4()),
            "user_id": buyer["cognito_sub"],
            "event_id": event_id,
            "refund_id": str(uuid.uuid4()),
            "amount": 17600,
            "currency": "SGD",
            "reason": "event_cancelled",
        },
    )

    wait_until(lambda: _subjects(buyer["email"]), "the refund email")
    assert _subjects(buyer["email"]) == ["Your refund of SGD 176.00 for your event"]
    _settle()
    assert _subjects(other["email"]) == []


def test_failed_refund_reaches_only_that_buyer(broker, people):
    event_id = str(uuid.uuid4())
    buyer, other = people.add(bought=(event_id,)), people.add(bought=(event_id,))

    broker.publish("RefundFailed", refund_failed_payload(buyer["cognito_sub"], event_id))

    wait_until(lambda: _subjects(buyer["email"]), "the failed-refund email")
    assert _subjects(buyer["email"]) == ["We couldn't complete your refund for your event"]
    _settle()
    assert _subjects(other["email"]) == []


def test_tickets_reach_the_buyer_with_a_qr_code_each(broker, people):
    buyer, other = people.add(), people.add()
    payload = {
        **ticket_issued_payload(buyer["cognito_sub"], str(uuid.uuid4())),
        "delivery_email": buyer["email"],
    }

    message_id = broker.publish("TicketIssued", payload)
    broker.publish("TicketIssued", payload, message_id=message_id)  # a redelivery

    wait_until(lambda: _subjects(buyer["email"]), "the ticket email")
    _settle()
    assert _subjects(buyer["email"]) == ["Your 2 tickets for Autumn Live"]
    assert _subjects(other["email"]) == []
    [email] = emails_to(buyer["email"])
    assert sorted(f["ContentID"] for f in inline_files(email["ID"])) == [
        "ticket-1",
        "ticket-2",
        "ticket-icon",
    ]


def test_poison_message_is_dead_lettered(broker):
    dlq = "notification-service.EventPublished.dlq"
    before = broker.message_count(dlq)
    broker.raw("event.events", "event.published", b"this is not json")
    wait_until(lambda: broker.message_count(dlq) == before + 1, "the poison message in the DLQ")


def test_duplicate_message_sends_one_email(broker, people):
    buyer = people.add()
    refund = {
        "order_id": str(uuid.uuid4()),
        "user_id": buyer["cognito_sub"],
        "event_id": str(uuid.uuid4()),
        "refund_id": str(uuid.uuid4()),
        "amount": 8800,
        "currency": "SGD",
        "reason": "refund_required",
    }
    message_id = broker.publish("OrderRefunded", refund)
    broker.publish("OrderRefunded", refund, message_id=message_id)  # redelivery
    broker.publish("OrderRefunded", refund)  # the same refund under a new message_id

    wait_until(lambda: _subjects(buyer["email"]), "the refund email")
    _settle()
    assert len(_subjects(buyer["email"])) == 1


def test_dead_letter_queues_hold_only_the_poison_test_message(broker):
    for queue in (
        "notification-service.EventCancelled",
        "notification-service.OrderRefunded",
        "notification-service.RefundFailed",
        "notification-service.TicketIssued",
    ):
        assert broker.message_count(f"{queue}.dlq") == 0, queue
