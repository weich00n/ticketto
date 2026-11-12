"""What checkout requires of each inbound message's `payload` (consumer-driven contract).

Checkout validates only the fields it acts on or cross-checks, and ignores every other
field (the broker contracts say consumers must ignore fields they don't know). A payload
missing a required field, or carrying the wrong type, can never be processed, so it is a
PoisonMessage: rejected without requeue, straight to the queue's DLQ, instead of being
redelivered five times first.

Sources of truth:
  EventCancelled     rabbitmq-service contracts/EventCancelled.payload.schema.json
                     (event_id, event_version, cancellation_id, cancelled_at, reason)
  RefundRequired     rabbitmq-service contracts/RefundRequired.payload.schema.json
                     (reservation-service OutboxWriter.refundRequired: hold_id, order_id,
                      user_id, event_id, total_amount, currency, reason, required_at)
  ReservationExpired rabbitmq-service contracts/ReservationExpired.payload.schema.json
                     (reservation-service HoldExpiryWorker); checkout needs only hold_id
"""

from __future__ import annotations

from dataclasses import dataclass

from .envelope import PoisonMessage

# Which message each checkout queue may carry (broker convention "<service>.<Message>").
QUEUE_MESSAGE_TYPES = {
    "checkout-service.EventCancelled": "EventCancelled",
    "checkout-service.ReservationExpired": "ReservationExpired",
    "checkout-service.RefundRequired": "RefundRequired",
}


@dataclass(frozen=True)
class RefundRequiredFields:
    order_id: str
    hold_id: str
    total_amount: int
    currency: str


def _text(payload: dict, field: str, message: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise PoisonMessage(f"{message} payload needs a non-empty string {field!r}")
    return value


def _int(payload: dict, field: str, message: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise PoisonMessage(f"{message} payload needs an integer {field!r}")
    return value


def event_cancelled_fields(payload: dict) -> str:
    """The cancelled event's id. event_version is not needed: cancellation is final."""
    return _text(payload, "event_id", "EventCancelled")


def reservation_expired_fields(payload: dict) -> str:
    """The expired hold's id."""
    return _text(payload, "hold_id", "ReservationExpired")


def refund_required_fields(payload: dict) -> RefundRequiredFields:
    """order_id to act on; hold_id, total_amount and currency to cross-check the order."""
    return RefundRequiredFields(
        order_id=_text(payload, "order_id", "RefundRequired"),
        hold_id=_text(payload, "hold_id", "RefundRequired"),
        total_amount=_int(payload, "total_amount", "RefundRequired"),
        currency=_text(payload, "currency", "RefundRequired"),
    )
