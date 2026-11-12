from datetime import UTC, datetime
from typing import Any

from .models import INVALIDATED, EventState, OutboxMessage, Ticket, TicketOrder
from .qr import QrSigner


def as_utc(value: datetime) -> datetime:
    # The database returns naive datetimes; everything is stored in UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return as_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def event_snapshot(state: EventState | None) -> dict[str, Any] | None:
    if state is None or not (state.title or state.venue or state.starts_at):
        return None
    return {"title": state.title, "venue": state.venue, "starts_at": iso_utc(state.starts_at)}


def ticket_response(ticket: Ticket, quantity: int, state: EventState | None) -> dict[str, Any]:
    return {
        "ticket_id": ticket.ticket_id,
        "order_id": ticket.order_id,
        "event_id": ticket.event_id,
        "category_id": ticket.category_id,
        "ticket_number": ticket.ticket_number,
        "tickets_in_order": quantity,
        "status": ticket.status,
        "event": event_snapshot(state),
        "category_name": ((state.category_names or {}) if state else {}).get(ticket.category_id),
        "qr_payload": None
        if ticket.status == INVALIDATED
        else QrSigner.payload(ticket.ticket_id, ticket.qr_key_id, ticket.qr_signature),
        "issued_at": iso_utc(ticket.issued_at),
        "checked_in_at": iso_utc(ticket.checked_in_at),
        "invalidated_at": iso_utc(ticket.invalidated_at),
        "invalidation_reason": ticket.invalidation_reason,
    }


def outbox_status(message: OutboxMessage) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "status": message.status,
        "attempts": message.attempts,
        "last_attempt_at": iso_utc(message.last_attempt_at),
        "published_at": iso_utc(message.published_at),
        "last_error": message.last_error,
    }


def outbox_message_response(message: OutboxMessage) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "message_type": message.message_type,
        "order_id": message.order_id,
        "status": message.status,
        "attempts": message.attempts,
        "created_at": iso_utc(message.created_at),
        "last_attempt_at": iso_utc(message.last_attempt_at),
        "next_attempt_at": iso_utc(message.next_attempt_at),
        "published_at": iso_utc(message.published_at),
        "last_error": message.last_error,
    }


def order_response(
    order: TicketOrder, tickets: list[Ticket], ticket_issued: OutboxMessage | None
) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "user_id": order.user_id,
        "event_id": order.event_id,
        "issuance_status": order.issuance_status,
        "rejection_reason": order.rejection_reason,
        "quantity": order.quantity,
        "tickets": [
            {"ticket_id": t.ticket_id, "ticket_number": t.ticket_number, "status": t.status}
            for t in tickets
        ],
        # The order's latest TicketIssued (a republish adds a newer one).
        "ticket_issued": None if ticket_issued is None else outbox_status(ticket_issued),
        "received_at": iso_utc(order.received_at),
    }
