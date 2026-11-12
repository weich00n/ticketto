"""Message payloads shaped like the publishers' (rabbitmq-service contracts/examples), shared
by the unit and integration tests."""

import uuid


def event_published_payload(event_id: str, title: str) -> dict:
    return {
        "event_id": event_id,
        "event_version": 2,
        "title": title,
        "starts_at": "2026-11-20T12:00:00Z",
        "release_at": "2026-10-20T12:00:00Z",
        "queue_join_opens_at": "2026-10-20T11:45:00Z",
        "published_at": "2026-10-06T10:05:00Z",
        "total_capacity": 500,
        "ticket_categories": [
            {
                "category_id": str(uuid.uuid4()),
                "name": "CAT 1",
                "price_amount": 8800,
                "currency": "SGD",
                "total_quantity": 500,
            }
        ],
    }


def ticket_issued_payload(user_sub: str, event_id: str, *, tickets: int = 2) -> dict:
    """ticket-service's TicketIssued payload (app/outbox.py). The QR codes are made up."""
    ticket_ids = [str(uuid.uuid4()) for _ in range(tickets)]
    return {
        "order_id": str(uuid.uuid4()),
        "user_id": user_sub,
        "event_id": event_id,
        "delivery_email": "buyer@example.com",
        "title": "Autumn Live",
        "venue": "National Stadium",
        "starts_at": "2026-11-20T12:00:00Z",
        "category_name": "CAT 1",
        "tickets": [
            {
                "ticket_id": ticket_id,
                "ticket_number": number,
                "qr_payload": f"TKT1.k1.{ticket_id}.{'a' * 43}",
            }
            for number, ticket_id in enumerate(ticket_ids, start=1)
        ],
    }


def refund_failed_payload(user_sub: str, event_id: str) -> dict:
    """checkout-service's RefundFailed payload (app/messaging/events.py refund_failed_data)."""
    return {
        **order_refunded_payload(user_sub, event_id),
        "last_error": "StripeRejectedError: charge_already_refunded for pi_secret",
    }


def order_refunded_payload(user_sub: str, event_id: str) -> dict:
    """checkout-service's OrderRefunded payload (app/messaging/events.py order_refunded_data)."""
    return {
        "order_id": str(uuid.uuid4()),
        "user_id": user_sub,
        "event_id": event_id,
        "refund_id": str(uuid.uuid4()),
        "amount": 8800,
        "currency": "SGD",
        "reason": "event_cancelled",
    }
