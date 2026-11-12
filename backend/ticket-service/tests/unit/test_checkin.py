import threading
import uuid

from sqlalchemy import func, select

from app.main import create_app
from app.models import CheckIn
from app.qr import QrSigner
from tests.unit.conftest import (
    buyer_headers,
    event_published,
    gateway_buyer_headers,
    gateway_staff_headers,
    order_payload,
    staff_headers,
)


def _issue(client, deliver, **overrides):
    payload = order_payload(**overrides)
    deliver("OrderConfirmed", payload)
    tickets = client.get(
        f"/api/v1/tickets?order_id={payload['order_id']}",
        headers=buyer_headers(payload["user_id"]),
    ).get_json()["items"]
    return payload, tickets


def _scan(client, qr_payload, event_id, headers=None):
    return client.post(
        "/api/v1/tickets/check-in",
        json={"qr_payload": qr_payload, "event_id": event_id},
        headers=staff_headers() if headers is None else headers,
    )


def test_valid_ticket_checks_in_once(client, deliver, session_factory):
    payload, tickets = _issue(client, deliver)
    deliver("EventPublished", event_published(payload["event_id"], payload["category_id"]))
    ticket = tickets[0]

    first = _scan(client, ticket["qr_payload"], payload["event_id"])
    second = _scan(client, ticket["qr_payload"], payload["event_id"])

    assert first.status_code == 200
    body = first.get_json()
    assert body["status"] == "CHECKED_IN"
    assert body["ticket_id"] == ticket["ticket_id"]
    assert body["category_name"] == "CAT 1"
    assert (body["ticket_number"], body["tickets_in_order"]) == (1, 2)
    assert second.status_code == 409
    error = second.get_json()["error"]
    assert error["code"] == "TICKET_ALREADY_CHECKED_IN"
    assert error["details"]["checked_in_at"] == body["checked_in_at"]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(CheckIn)) == 1
        check_in = session.scalars(select(CheckIn)).one()
        assert check_in.checked_in_by == "gate-1"

    shown = client.get(f"/api/v1/tickets/{ticket['ticket_id']}", headers=buyer_headers())
    assert shown.get_json()["status"] == "CHECKED_IN"
    # The other ticket in the order is unaffected.
    assert _scan(client, tickets[1]["qr_payload"], payload["event_id"]).status_code == 200


def test_concurrent_scans_of_one_ticket_admit_exactly_one(app, client, deliver):
    payload, tickets = _issue(client, deliver, quantity=1)
    results: list[int] = []
    barrier = threading.Barrier(8)

    def scan():
        with app.test_client() as own_client:
            barrier.wait()
            response = _scan(own_client, tickets[0]["qr_payload"], payload["event_id"])
            results.append(response.status_code)

    threads = [threading.Thread(target=scan) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [200] + [409] * 7


def test_check_in_needs_no_authentication_yet(client, deliver):
    payload, tickets = _issue(client, deliver)

    anonymous = _scan(client, tickets[0]["qr_payload"], payload["event_id"], {})

    assert anonymous.status_code == 200


def test_with_gateway_auth_only_organisers_or_staff_can_check_in(client, gateway_client, deliver):
    payload, tickets = _issue(client, deliver)
    first, second = (t["qr_payload"] for t in tickets)

    as_buyer = _scan(gateway_client, first, payload["event_id"], gateway_buyer_headers())
    anonymous = _scan(gateway_client, first, payload["event_id"], {})
    as_organiser = _scan(gateway_client, first, payload["event_id"], gateway_staff_headers())
    as_staff = _scan(gateway_client, second, payload["event_id"], gateway_staff_headers("staff"))

    assert as_buyer.status_code == 403
    assert as_buyer.get_json()["error"]["code"] == "FORBIDDEN"
    assert anonymous.status_code == 401
    assert as_organiser.status_code == 200
    assert as_staff.status_code == 200


def test_check_in_groups_are_configurable(settings, deliver, client):
    payload, tickets = _issue(client, deliver)
    only_staff = create_app(
        type(settings)(
            **{**settings.__dict__, "auth_mode": "gateway", "check_in_groups": frozenset({"staff"})}
        )
    ).test_client()

    response = _scan(
        only_staff, tickets[0]["qr_payload"], payload["event_id"], gateway_staff_headers()
    )

    assert response.status_code == 403


def test_forged_or_malformed_codes_are_rejected(client, deliver):
    payload, tickets = _issue(client, deliver)
    genuine = tickets[0]["qr_payload"]
    forged = genuine[:-2] + ("AA" if not genuine.endswith("AA") else "BB")

    for code in (forged, "hello", genuine.replace("TKT1", "TKT0")):
        response = _scan(client, code, payload["event_id"])
        assert response.status_code == 400, code
        assert response.get_json()["error"]["code"] == "INVALID_QR_CODE"


def test_genuine_signature_for_an_unknown_ticket_is_not_found(client, signer, deliver):
    payload, _ = _issue(client, deliver)
    ticket_id = str(uuid.uuid4())
    code = QrSigner.payload(ticket_id, *signer.sign(ticket_id))

    response = _scan(client, code, payload["event_id"])

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "TICKET_NOT_FOUND"


def test_ticket_for_another_event_is_rejected(client, deliver):
    payload, tickets = _issue(client, deliver)
    other_event = str(uuid.uuid4())

    response = _scan(client, tickets[0]["qr_payload"], other_event)

    assert response.status_code == 409
    error = response.get_json()["error"]
    assert error["code"] == "TICKET_EVENT_MISMATCH"
    assert error["details"]["ticket_event_id"] == payload["event_id"]


def test_tickets_of_a_cancelled_event_are_rejected(client, deliver):
    payload, tickets = _issue(client, deliver)
    qr_payload = tickets[0]["qr_payload"]  # captured before cancellation
    deliver("EventCancelled", {"event_id": payload["event_id"]})

    response = _scan(client, qr_payload, payload["event_id"])

    assert response.status_code == 409
    assert response.get_json()["error"]["details"] == {"invalidation_reason": "EVENT_CANCELLED"}


def test_refunded_tickets_are_rejected(client, deliver):
    payload, tickets = _issue(client, deliver)
    deliver("RefundCompleted", {"order_id": payload["order_id"], "event_id": payload["event_id"]})

    response = _scan(client, tickets[0]["qr_payload"], payload["event_id"])

    assert response.status_code == 409
    assert response.get_json()["error"]["details"] == {"invalidation_reason": "ORDER_REFUNDED"}


def test_check_in_body_is_validated(client):
    missing = client.post("/api/v1/tickets/check-in", json={}, headers=staff_headers())
    bad_event = client.post(
        "/api/v1/tickets/check-in",
        json={"qr_payload": "TKT1.x", "event_id": "nope"},
        headers=staff_headers(),
    )
    not_json = client.post("/api/v1/tickets/check-in", data="x", headers=staff_headers())

    assert missing.status_code == 400
    assert set(missing.get_json()["error"]["details"]) == {"qr_payload", "event_id"}
    assert bad_event.status_code == 400
    assert bad_event.get_json()["error"]["details"] == {"event_id": "Must be a UUID."}
    assert not_json.status_code == 400
