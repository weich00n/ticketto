"""The notifications API against the real stack: messages published to the broker become
notifications the signed-in user can list and mark read. PLAN.md test 12."""

import json
import uuid

import pytest

from tests.integration.conftest import API_URL, http, wait_until
from tests.payloads import event_published_payload, order_refunded_payload, ticket_issued_payload

pytestmark = pytest.mark.integration


def as_user(cognito_sub: str, method: str, path: str) -> tuple[int, dict]:
    """A real request, with the header API Gateway sets from the verified token."""
    return http(method, f"{API_URL}{path}", headers={"X-User-Sub": cognito_sub})


def test_users_see_only_their_own_notifications_and_can_mark_them_read(broker, people):
    event_id = str(uuid.uuid4())
    buyer = people.add(bought=(event_id,))
    other = people.add(notify=False)
    title = f"IT Inbox {uuid.uuid4().hex[:6]}"
    broker.publish("EventPublished", event_published_payload(event_id, title))
    broker.publish("OrderRefunded", order_refunded_payload(buyer["cognito_sub"], event_id))

    def listed():
        _, body = as_user(buyer["cognito_sub"], "GET", "/api/v1/notifications")
        return body if body["total_items"] == 2 else None

    body = wait_until(listed, "both notifications in the buyer's inbox")
    assert {item["kind"] for item in body["items"]} == {"EVENT_PUBLISHED", "ORDER_REFUNDED"}
    assert any(item["title"] == f"New on Ticketto: {title}" for item in body["items"])

    _, theirs = as_user(other["cognito_sub"], "GET", "/api/v1/notifications")
    assert theirs["total_items"] == 0  # opted out, bought nothing

    _, count = as_user(buyer["cognito_sub"], "GET", "/api/v1/notifications/unread-count")
    assert count == {"unread": 2}
    status, _ = as_user(buyer["cognito_sub"], "POST", "/api/v1/notifications/read-all")
    assert status == 204
    _, count = as_user(buyer["cognito_sub"], "GET", "/api/v1/notifications/unread-count")
    assert count == {"unread": 0}


def test_a_ticket_notification_is_listed_without_its_qr_codes(broker, people):
    buyer = people.add()
    payload = {
        **ticket_issued_payload(buyer["cognito_sub"], str(uuid.uuid4())),
        "delivery_email": buyer["email"],
    }
    broker.publish("TicketIssued", payload)

    def listed():
        _, body = as_user(buyer["cognito_sub"], "GET", "/api/v1/notifications")
        return body if body["total_items"] == 1 else None

    body = wait_until(listed, "the ticket notification in the buyer's inbox")
    [item] = body["items"]
    assert item["kind"] == "TICKET_ISSUED"
    assert item["title"] == "Your 2 tickets for Autumn Live"
    assert item["order_id"] == payload["order_id"]
    assert "TKT1." not in json.dumps(body)
