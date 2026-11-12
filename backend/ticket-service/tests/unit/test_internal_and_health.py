from sqlalchemy.exc import OperationalError

from app.outbox import run_once
from tests.unit.conftest import new_id, order_payload
from tests.unit.test_outbox import FakePublisher


def test_health_and_readiness(client):
    assert client.get("/health").get_json() == {"status": "ok", "service": "ticket-service"}
    assert client.get("/ready").get_json() == {"status": "ready"}


def test_readiness_fails_without_a_database(client, app, monkeypatch):
    def broken():
        raise OperationalError("SELECT 1", {}, Exception("gone"))

    monkeypatch.setitem(app.extensions, "session_factory", broken)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.get_json() == {"status": "not_ready", "failed": ["database"]}


def test_database_outage_returns_503(client, app, monkeypatch):
    def broken():
        raise OperationalError("SELECT", {}, Exception("gone"))

    monkeypatch.setitem(app.extensions, "session_factory", broken)

    response = client.get("/api/v1/tickets", headers={"X-User-Id": new_id()})

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "SERVICE_UNAVAILABLE"


def test_order_tickets(client, deliver):
    payload = order_payload(quantity=2)
    deliver("OrderConfirmed", payload)

    response = client.get(f"/internal/v1/orders/{payload['order_id']}/tickets")

    body = response.get_json()
    assert response.status_code == 200
    assert body["issuance_status"] == "ISSUED" and body["rejection_reason"] is None
    assert [t["ticket_number"] for t in body["tickets"]] == [1, 2]
    assert body["ticket_issued"]["status"] == "PENDING"
    assert body["ticket_issued"]["published_at"] is None
    assert body["user_id"] == payload["user_id"]


def test_rejected_order_is_reported(client, deliver):
    payload = order_payload()
    deliver("EventCancelled", {"event_id": payload["event_id"]})
    deliver("OrderConfirmed", payload)

    body = client.get(f"/internal/v1/orders/{payload['order_id']}/tickets").get_json()

    assert (body["issuance_status"], body["rejection_reason"]) == ("REJECTED", "EVENT_CANCELLED")
    assert body["tickets"] == [] and body["ticket_issued"] is None


def test_unknown_order(client):
    response = client.get(f"/internal/v1/orders/{new_id()}/tickets")

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "ORDER_NOT_FOUND"


def test_unpublished_messages_are_listed_with_their_last_error(
    client, deliver, session_factory, settings
):
    payload = order_payload()
    deliver("OrderConfirmed", payload)
    run_once(session_factory, settings, FakePublisher(fail_times=1))

    pending = client.get("/internal/v1/outbox-messages").get_json()
    published = client.get("/internal/v1/outbox-messages?status=PUBLISHED").get_json()

    assert pending["count"] == 1 and published["count"] == 0
    item = pending["items"][0]
    assert (item["order_id"], item["message_type"], item["status"]) == (
        payload["order_id"],
        "TicketIssued",
        "PENDING",
    )
    assert item["attempts"] == 1 and "broker unavailable" in item["last_error"]
    assert item["next_attempt_at"].endswith("Z")


def test_republish_adds_a_new_message_once_the_first_was_published(
    client, deliver, session_factory, settings
):
    payload = order_payload()
    deliver("OrderConfirmed", payload)
    publisher = FakePublisher()
    run_once(session_factory, settings, publisher)
    first_id = publisher.published[0]["message_id"]

    response = client.post(f"/internal/v1/orders/{payload['order_id']}/ticket-issued/republish")
    run_once(session_factory, settings, publisher)

    body = response.get_json()
    assert response.status_code == 202
    assert body["order_id"] == payload["order_id"] and body["status"] == "PENDING"
    assert body["message_id"] != first_id  # not deduplicated as the first one
    assert [m["message_id"] for m in publisher.published] == [first_id, body["message_id"]]
    order = client.get(f"/internal/v1/orders/{payload['order_id']}/tickets").get_json()
    assert order["ticket_issued"]["message_id"] == body["message_id"]
    assert order["ticket_issued"]["status"] == "PUBLISHED"


def test_republish_of_a_waiting_message_makes_it_due_instead_of_adding_one(
    client, deliver, session_factory, settings
):
    payload = order_payload()
    deliver("OrderConfirmed", payload)
    run_once(session_factory, settings, FakePublisher(fail_times=1))  # now waiting to retry

    first = client.post(f"/internal/v1/orders/{payload['order_id']}/ticket-issued/republish")
    second = client.post(f"/internal/v1/orders/{payload['order_id']}/ticket-issued/republish")
    publisher = FakePublisher()
    run_once(session_factory, settings, publisher)

    assert first.get_json()["message_id"] == second.get_json()["message_id"]
    assert len(publisher.published) == 1


def test_cancelled_order_cannot_be_republished(client, deliver):
    payload = order_payload()
    deliver("OrderConfirmed", payload)
    deliver("EventCancelled", {"event_id": payload["event_id"]})

    response = client.post(f"/internal/v1/orders/{payload['order_id']}/ticket-issued/republish")

    assert response.status_code == 409
    error = response.get_json()["error"]
    assert error["code"] == "TICKET_ISSUED_NOT_REPUBLISHABLE"
    assert error["details"] == {"issuance_status": "INVALIDATED", "valid_tickets": 0}


def test_outbox_filters_are_validated(client):
    assert client.get("/internal/v1/outbox-messages?status=LOST").status_code == 400
    assert client.get("/internal/v1/outbox-messages?limit=500").status_code == 400
    response = client.post(f"/internal/v1/orders/{new_id()}/ticket-issued/republish")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "ORDER_NOT_FOUND"


def test_rejected_order_has_nothing_to_republish(client, deliver):
    payload = order_payload()
    deliver("EventCancelled", {"event_id": payload["event_id"]})
    deliver("OrderConfirmed", payload)

    response = client.post(f"/internal/v1/orders/{payload['order_id']}/ticket-issued/republish")

    assert response.status_code == 409
    assert response.get_json()["error"]["details"]["issuance_status"] == "REJECTED"


def test_order_whose_tickets_were_all_used_has_nothing_to_republish(client, deliver):
    payload = order_payload(quantity=1)
    deliver("OrderConfirmed", payload)
    ticket = client.get(
        f"/api/v1/tickets?order_id={payload['order_id']}",
        headers={"X-User-Id": payload["user_id"]},
    ).get_json()["items"][0]
    client.post(
        "/api/v1/tickets/check-in",
        json={"qr_payload": ticket["qr_payload"], "event_id": payload["event_id"]},
    )

    response = client.post(f"/internal/v1/orders/{payload['order_id']}/ticket-issued/republish")

    assert response.status_code == 409
    assert response.get_json()["error"]["details"] == {
        "issuance_status": "ISSUED",
        "valid_tickets": 0,
    }
