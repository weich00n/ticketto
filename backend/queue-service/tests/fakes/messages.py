"""Builders for every message Queue consumes, in the real envelope format and with
the real exchange and routing key from the contracts doc. A contract change
should break these, and therefore the tests.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.timefmt import to_iso

DEFAULT_RELEASE = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
DEFAULT_JOIN_LEAD = timedelta(minutes=15)


def _iso_ms(dt: datetime) -> str:
    """ISO 8601 with milliseconds, as a producer might send (Queue accepts any offset)."""
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class OutgoingMessage:
    exchange: str
    routing_key: str
    envelope: dict[str, Any] = field(default_factory=dict)

    @property
    def message_id(self) -> str:
        return str(self.envelope["message_id"])

    @property
    def message_type(self) -> str:
        return str(self.envelope["message_type"])

    @property
    def body(self) -> bytes:
        return json.dumps(self.envelope).encode()

    def with_envelope(self, **changes: Any) -> OutgoingMessage:
        return replace(self, envelope={**self.envelope, **changes})

    def with_payload(self, **changes: Any) -> OutgoingMessage:
        return self.with_envelope(payload={**self.envelope["payload"], **changes})


def _message(
    exchange: str,
    routing_key: str,
    message_type: str,
    producer: str,
    payload: dict[str, Any],
    message_id: str | None,
) -> OutgoingMessage:
    return OutgoingMessage(
        exchange,
        routing_key,
        {
            "message_id": message_id or str(uuid.uuid4()),
            "message_type": message_type,
            "schema_version": 1,
            "occurred_at": to_iso(datetime.now(UTC)),
            "producer": producer,
            "payload": payload,
        },
    )


def event_published(
    event_id: str,
    *,
    version: int = 1,
    total_capacity: int = 304,
    release_at: datetime = DEFAULT_RELEASE,
    queue_join_opens_at: datetime | None = None,  # default: release_at - 15 min
    sale_ends_at: datetime | None = None,
    message_id: str | None = None,
) -> OutgoingMessage:
    opens = release_at - DEFAULT_JOIN_LEAD if queue_join_opens_at is None else queue_join_opens_at
    payload: dict[str, Any] = {
        "event_id": event_id,
        "event_version": version,
        "total_capacity": total_capacity,
        "queue_join_opens_at": _iso_ms(opens),
        "release_at": _iso_ms(release_at),
    }
    if sale_ends_at is not None:
        payload["starts_at"] = _iso_ms(sale_ends_at)
    return _message(
        "event.events", "event.published", "EventPublished", "event-service", payload, message_id
    )


def event_updated(
    event_id: str,
    *,
    version: int,
    total_capacity: int = 304,
    release_at: datetime = DEFAULT_RELEASE,
    queue_join_opens_at: datetime | None = None,
    sale_ends_at: datetime | None = None,
    message_id: str | None = None,
) -> OutgoingMessage:
    msg = event_published(
        event_id,
        version=version,
        total_capacity=total_capacity,
        release_at=release_at,
        queue_join_opens_at=queue_join_opens_at,
        sale_ends_at=sale_ends_at,
        message_id=message_id,
    )
    return replace(msg, routing_key="event.updated").with_envelope(message_type="EventUpdated")


def event_cancelled(
    event_id: str,
    *,
    version: int,
    cancelled_at: datetime | None = None,
    message_id: str | None = None,
) -> OutgoingMessage:
    payload = {
        "event_id": event_id,
        "event_version": version,
        "cancelled_at": _iso_ms(cancelled_at or datetime.now(UTC)),
    }
    return _message(
        "event.events", "event.cancelled", "EventCancelled", "event-service", payload, message_id
    )


def event_sold_out(
    event_id: str, *, sold_out_at: datetime | None = None, message_id: str | None = None
) -> OutgoingMessage:
    payload = {"event_id": event_id, "sold_out_at": _iso_ms(sold_out_at or datetime.now(UTC))}
    return _message(
        "reservation.events",
        "event.sold_out",
        "EventSoldOut",
        "reservation-service",
        payload,
        message_id,
    )


def order_confirmed(
    *,
    admission_id: str,
    user_id: str,
    event_id: str,
    order_id: str | None = None,
    confirmed_at: datetime | None = None,
    message_id: str | None = None,
) -> OutgoingMessage:
    payload = {
        "order_id": order_id or str(uuid.uuid4()),
        "admission_id": admission_id,
        "user_id": user_id,
        "event_id": event_id,
        "confirmed_at": _iso_ms(confirmed_at or datetime.now(UTC)),
    }
    return _message(
        "checkout.events",
        "order.confirmed",
        "OrderConfirmed",
        "checkout-service",
        payload,
        message_id,
    )
