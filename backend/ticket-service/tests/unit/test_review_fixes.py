"""Regression tests for issues found in review."""

from app import issuance
from app.auth import _groups
from app.correlation import get_correlation_id
from app.issuance import utcnow
from app.messages import parse_message
from app.models import OutboxMessage, ProcessedMessage
from app.outbox import run_once
from tests.unit.conftest import envelope, event_published, new_id, order_payload
from tests.unit.test_outbox import FakePublisher, outbox_row


def test_every_outcome_fits_the_outcome_column(deliver, session_factory):
    # MySQL rejects values longer than VARCHAR(32); SQLite would not notice.
    payload = order_payload()
    outcome = deliver(
        "RefundCompleted", {"order_id": payload["order_id"], "event_id": payload["event_id"]}
    )
    assert outcome == "refund_before_order"
    with session_factory() as session:
        lengths = [len(m.outcome) for m in session.query(ProcessedMessage).all()]
    assert max(lengths) <= 32


def test_unpublished_message_of_an_invalidated_order_is_cancelled_and_not_republishable(
    client, deliver, session_factory, settings
):
    payload = order_payload()
    deliver("OrderConfirmed", payload)
    run_once(session_factory, settings, FakePublisher(fail_times=1))  # waiting to retry
    deliver("RefundCompleted", {"order_id": payload["order_id"], "event_id": payload["event_id"]})

    assert outbox_row(session_factory, payload["order_id"]).status == "CANCELLED"
    pending = client.get("/internal/v1/outbox-messages").get_json()
    republish = client.post(f"/internal/v1/orders/{payload['order_id']}/ticket-issued/republish")
    assert pending["count"] == 0
    assert republish.status_code == 409


def test_published_message_of_an_invalidated_order_is_not_republished(
    client, deliver, session_factory, settings
):
    payload = order_payload()
    deliver("OrderConfirmed", payload)
    run_once(session_factory, settings, FakePublisher())
    deliver("EventCancelled", {"event_id": payload["event_id"]})

    republish = client.post(f"/internal/v1/orders/{payload['order_id']}/ticket-issued/republish")

    assert republish.status_code == 409
    assert outbox_row(session_factory, payload["order_id"]).status == "PUBLISHED"


def test_a_claim_that_never_finished_is_retried_after_its_lease_with_the_same_id(
    deliver, session_factory, settings
):
    payload = order_payload()
    deliver("OrderConfirmed", payload)
    with session_factory() as session, session.begin():
        row = session.get(OutboxMessage, outbox_row(session_factory, payload["order_id"]).id)
        row.attempts = 7  # earlier attempts crashed mid-way; there is no limit to give up at
        row.next_attempt_at = utcnow()
    publisher = FakePublisher()

    assert run_once(session_factory, settings, publisher) == 1

    row = outbox_row(session_factory, payload["order_id"])
    assert (row.status, row.attempts) == ("PUBLISHED", 8)
    assert publisher.published[0]["message_id"] == row.message_id


def test_group_header_formats():
    assert _groups("[staff]") == {"staff"}
    assert _groups("[buyer, staff]") == {"buyer", "staff"}
    assert _groups("[buyer staff]") == {"buyer", "staff"}
    assert _groups('["buyer","staff"]') == {"buyer", "staff"}
    assert _groups("") == frozenset()


def test_empty_filters_are_ignored(client, deliver):
    deliver("OrderConfirmed", order_payload(quantity=1))

    response = client.get(
        "/api/v1/tickets?event_id=&order_id=&status=",
        headers={"X-User-Id": order_payload()["user_id"]},
    )

    assert response.status_code == 200
    assert response.get_json()["total_items"] == 1


def test_non_ascii_digits_are_a_validation_error(client):
    response = client.get("/api/v1/tickets?page=²", headers={"X-User-Id": new_id()})

    assert response.status_code == 400


def test_one_bad_category_does_not_reject_the_snapshot():
    event_id, category_id = new_id(), new_id()
    payload = event_published(event_id, category_id)
    payload["ticket_categories"].append({"category_id": "not-a-uuid", "name": "Broken"})

    message = parse_message(envelope("EventPublished", payload))

    assert [c.category_id for c in message.payload.ticket_categories] == [category_id]


def test_message_logs_use_the_message_id_without_any_correlation_id(deliver, monkeypatch):
    seen = []
    monkeypatch.setattr(
        issuance.logger, "info", lambda *_a, **_k: seen.append(get_correlation_id())
    )
    message_id = new_id()

    deliver("OrderConfirmed", order_payload(), message_id)

    assert seen == [message_id]
