from tests.unit.conftest import (
    BUYER,
    OTHER_BUYER,
    buyer_headers,
    event_published,
    gateway_buyer_headers,
    new_id,
    order_payload,
)


def test_buyer_lists_only_their_own_tickets(client, deliver):
    mine = order_payload(quantity=2)
    theirs = order_payload(user_id=OTHER_BUYER, quantity=1)
    deliver("OrderConfirmed", mine)
    deliver("OrderConfirmed", theirs)

    response = client.get("/api/v1/tickets", headers=buyer_headers(BUYER))

    assert response.status_code == 200
    body = response.get_json()
    assert body["total_items"] == 2 and body["page"] == 1 and body["page_size"] == 20
    assert {t["order_id"] for t in body["items"]} == {mine["order_id"]}
    first = body["items"][0]
    assert first["ticket_number"] == 1 and first["tickets_in_order"] == 2
    assert first["status"] == "VALID"
    assert first["qr_payload"].startswith("TKT1.k1.")
    assert "email_delivery_status" not in first  # the email is notification-service's now
    assert first["issued_at"].endswith("Z")


def test_tickets_carry_the_cached_event_snapshot(client, deliver):
    payload = order_payload()
    deliver("EventPublished", event_published(payload["event_id"], payload["category_id"]))
    deliver("OrderConfirmed", payload)

    ticket = client.get("/api/v1/tickets", headers=buyer_headers()).get_json()["items"][0]

    assert ticket["event"]["title"] == "The Midnight Symphony"
    assert ticket["event"]["venue"] is None  # EventPublished doesn't carry venue yet
    assert ticket["category_name"] == "CAT 1"


def test_filters_and_pagination(client, deliver):
    a = order_payload(quantity=3)
    b = order_payload(quantity=1)
    deliver("OrderConfirmed", a)
    deliver("OrderConfirmed", b)
    deliver("EventCancelled", {"event_id": b["event_id"]})
    headers = buyer_headers()

    by_order = client.get(f"/api/v1/tickets?order_id={a['order_id']}", headers=headers).get_json()
    by_event = client.get(f"/api/v1/tickets?event_id={b['event_id']}", headers=headers).get_json()
    invalid = client.get("/api/v1/tickets?status=INVALIDATED", headers=headers).get_json()
    page2 = client.get("/api/v1/tickets?page=2&page_size=3", headers=headers).get_json()

    assert by_order["total_items"] == 3
    assert by_event["total_items"] == 1
    assert [t["order_id"] for t in invalid["items"]] == [b["order_id"]]
    assert invalid["items"][0]["qr_payload"] is None
    assert invalid["items"][0]["invalidation_reason"] == "EVENT_CANCELLED"
    assert page2["total_items"] == 4 and len(page2["items"]) == 1


def test_empty_list_is_not_an_error(client):
    response = client.get("/api/v1/tickets", headers=buyer_headers())

    assert response.status_code == 200
    assert response.get_json()["items"] == []


def test_invalid_query_parameters(client):
    headers = buyer_headers()
    for query, field in [
        ("status=USED", "status"),
        ("page=0", "page"),
        ("page_size=101", "page_size"),
        ("event_id=42", "event_id"),
    ]:
        response = client.get(f"/api/v1/tickets?{query}", headers=headers)
        body = response.get_json()
        assert response.status_code == 400, query
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert field in body["error"]["details"]


def test_identity_headers_are_required(client):
    response = client.get("/api/v1/tickets", headers={"X-Correlation-Id": "trace-1"})

    assert response.status_code == 401
    assert response.get_json() == {
        "error": {"code": "UNAUTHENTICATED", "message": "The X-User-Id header is required."},
        "correlation_id": "trace-1",
    }
    assert response.headers["X-Correlation-Id"] == "trace-1"


def test_get_ticket(client, deliver):
    payload = order_payload(quantity=1)
    deliver("OrderConfirmed", payload)
    ticket_id = client.get("/api/v1/tickets", headers=buyer_headers()).get_json()["items"][0][
        "ticket_id"
    ]

    response = client.get(f"/api/v1/tickets/{ticket_id}", headers=buyer_headers())

    assert response.status_code == 200
    assert response.get_json()["ticket_id"] == ticket_id


def test_another_buyers_ticket_is_not_found(client, deliver):
    deliver("OrderConfirmed", order_payload(quantity=1))
    ticket_id = client.get("/api/v1/tickets", headers=buyer_headers()).get_json()["items"][0][
        "ticket_id"
    ]

    response = client.get(f"/api/v1/tickets/{ticket_id}", headers=buyer_headers(OTHER_BUYER))

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "TICKET_NOT_FOUND"


def test_malformed_and_unknown_ticket_ids(client):
    assert client.get("/api/v1/tickets/abc", headers=buyer_headers()).status_code == 400
    assert client.get(f"/api/v1/tickets/{new_id()}", headers=buyer_headers()).status_code == 404


def test_qr_image(client, deliver, signer):
    deliver("OrderConfirmed", order_payload(quantity=1))
    ticket = client.get("/api/v1/tickets", headers=buyer_headers()).get_json()["items"][0]

    response = client.get(
        f"/api/v1/tickets/{ticket['ticket_id']}/qr?scale=4", headers=buyer_headers()
    )

    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.data.startswith(b"\x89PNG")
    assert signer.verify(ticket["qr_payload"]).ticket_id == ticket["ticket_id"]


def test_qr_image_for_invalidated_ticket_is_refused(client, deliver):
    payload = order_payload(quantity=1)
    deliver("OrderConfirmed", payload)
    deliver("EventCancelled", {"event_id": payload["event_id"]})
    ticket_id = client.get("/api/v1/tickets", headers=buyer_headers()).get_json()["items"][0][
        "ticket_id"
    ]

    response = client.get(f"/api/v1/tickets/{ticket_id}/qr", headers=buyer_headers())

    assert response.status_code == 409
    assert response.get_json()["error"] == {
        "code": "TICKET_INVALIDATED",
        "message": "This ticket is no longer valid.",
        "details": {"invalidation_reason": "EVENT_CANCELLED"},
    }


def test_qr_scale_is_validated(client, deliver):
    deliver("OrderConfirmed", order_payload(quantity=1))
    ticket_id = client.get("/api/v1/tickets", headers=buyer_headers()).get_json()["items"][0][
        "ticket_id"
    ]

    response = client.get(f"/api/v1/tickets/{ticket_id}/qr?scale=50", headers=buyer_headers())

    assert response.status_code == 400


def test_gateway_mode_uses_the_cognito_subject(client, gateway_client, deliver):
    payload = order_payload(quantity=1)
    deliver("OrderConfirmed", payload)

    with_sub = gateway_client.get("/api/v1/tickets", headers=gateway_buyer_headers())
    plain_header = gateway_client.get("/api/v1/tickets", headers=buyer_headers())

    assert with_sub.get_json()["total_items"] == 1
    assert plain_header.status_code == 401  # X-User-Id is not trusted behind the gateway
    assert client.get("/api/v1/tickets", headers=buyer_headers()).get_json()["total_items"] == 1
