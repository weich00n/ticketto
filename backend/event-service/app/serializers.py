from datetime import UTC, datetime
from typing import Any

from .models import Event, TicketCategory
from .schemas import queue_join_opens_at


def as_utc(value: datetime) -> datetime:
    # The database returns naive datetimes; everything is stored in UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return as_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def category_response(category: TicketCategory) -> dict[str, Any]:
    return {
        "category_id": category.category_id,
        "name": category.name,
        "price_amount": category.price_amount,
        "currency": category.currency,
        "total_quantity": category.total_quantity,
    }


def image_url(event: Event) -> str | None:
    if not event.image_version:
        return None
    return f"/api/v1/events/{event.event_id}/image?v={event.image_version}"


def event_response(event: Event) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "title": event.title,
        "description": event.description,
        "venue": event.venue,
        "category": event.category,
        "starts_at": iso_utc(event.starts_at),
        "ticket_release_at": iso_utc(event.ticket_release_at),
        "queue_join_opens_at": iso_utc(queue_join_opens_at(as_utc(event.ticket_release_at))),
        "status": event.status,
        "image_url": image_url(event),
        "ticket_categories": [category_response(c) for c in event.ticket_categories],
    }


def organiser_event_response(event: Event) -> dict[str, Any]:
    return event_response(event) | {
        "organiser_id": event.organiser_id,
        "created_at": iso_utc(event.created_at),
        "updated_at": iso_utc(event.updated_at),
        "published_at": iso_utc(event.published_at),
        "cancelled_at": iso_utc(event.cancelled_at),
        "cancellation_reason": event.cancellation_reason,
    }
