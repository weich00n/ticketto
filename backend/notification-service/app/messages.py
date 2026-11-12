"""The broker envelope and the payloads notification-service consumes.

The envelope follows rabbitmq-service contracts/envelope.schema.json: exactly six fields. The
correlation id travels as the AMQP correlation_id property, not in the body. Payloads follow
contracts/EventPublished.payload.schema.json, contracts/EventCancelled.payload.schema.json,
contracts/TicketIssued.payload.schema.json and checkout-service's OrderRefunded
(app/messaging/events.py order_refunded_data); unknown payload fields are ignored.

parse() raises PoisonMessage for anything that can never be processed (bad JSON, wrong
shape, unknown schema_version, invalid payload): the consumer dead-letters it at once. A
message type this service doesn't use parses with payload None, and is acked and ignored.
"""

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
)

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_MESSAGE_TYPE = re.compile(r"^[A-Z][A-Za-z]+$")
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
_PRODUCER = re.compile(r"^[a-z]+(-[a-z]+)*-service$")

ENVELOPE_FIELDS = (
    "message_id",
    "message_type",
    "schema_version",
    "occurred_at",
    "producer",
    "payload",
)
SUPPORTED_SCHEMA_VERSIONS = (1,)


class PoisonMessage(ValueError):
    """The message can never be processed; dead-letter it."""


def _canonical_uuid(value: str) -> str:
    return str(uuid.UUID(str(value)))


def parse_utc(value: Any) -> datetime:
    """'2026-11-20T12:00:00Z' -> naive UTC datetime (every time in this service is naive UTC)."""
    if not isinstance(value, str) or not _UTC.match(value):
        raise ValueError("must be a UTC time like 2026-11-20T12:00:00Z")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC).replace(tzinfo=None, microsecond=0)


Uuid = Annotated[str, AfterValidator(_canonical_uuid)]
UtcTime = Annotated[datetime, BeforeValidator(parse_utc)]
# A buyer's Cognito sub: checkout-service uses X-User-Sub as user_id, and ticket-service keeps it.
BuyerSub = Annotated[str, Field(min_length=1, max_length=64), AfterValidator(str.lower)]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, str_strip_whitespace=True)


class EventPublished(_Payload):
    event_id: Uuid
    title: Annotated[str, Field(min_length=1, max_length=160)]
    starts_at: UtcTime
    release_at: UtcTime | None = None
    queue_join_opens_at: UtcTime | None = None


class EventCancelled(_Payload):
    event_id: Uuid
    reason: Annotated[str, Field(max_length=500)] | None = None
    cancelled_at: UtcTime | None = None


class OrderRefunded(_Payload):
    order_id: Uuid
    user_id: BuyerSub
    event_id: Uuid
    refund_id: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    amount: Annotated[StrictInt, Field(ge=0)]  # minor units: 8800 = SGD 88.00
    currency: Annotated[str, Field(min_length=3, max_length=3)] = "SGD"
    reason: Annotated[str, Field(max_length=64)] | None = None  # e.g. "event_cancelled"


class RefundFailed(OrderRefunded):
    """OrderRefunded's fields. checkout-service also sends last_error, raw Stripe text that
    must not reach a buyer: it isn't a field here, so it's dropped on parsing."""

    refund_id: Annotated[str, Field(min_length=1, max_length=64)]  # one email per refund


class IssuedTicket(_Payload):
    ticket_id: Uuid
    ticket_number: Annotated[StrictInt, Field(ge=1)]
    # Works like the ticket itself: never log it. Only ticket-service can verify it.
    qr_payload: Annotated[str, Field(min_length=1, max_length=200)]


class TicketIssued(_Payload):
    """One per order. ticket-service sends null for what it doesn't know."""

    order_id: Uuid
    user_id: BuyerSub
    event_id: Uuid
    delivery_email: Annotated[str, Field(max_length=320)] | None = None
    title: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    venue: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    starts_at: UtcTime | None = None
    category_name: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    tickets: Annotated[list[IssuedTicket], Field(min_length=1, max_length=5)]


Payload = EventPublished | EventCancelled | OrderRefunded | RefundFailed | TicketIssued

PAYLOAD_TYPES: dict[str, type[_Payload]] = {
    "EventPublished": EventPublished,
    "EventCancelled": EventCancelled,
    "OrderRefunded": OrderRefunded,
    "RefundFailed": RefundFailed,
    "TicketIssued": TicketIssued,
}


@dataclass(frozen=True)
class Message:
    message_id: str
    message_type: str
    payload: Payload | None  # None: a message type this service doesn't use
    correlation_id: str | None = None


def _check_envelope(raw: dict) -> None:
    missing = [k for k in ENVELOPE_FIELDS if k not in raw]
    if missing:
        raise PoisonMessage(f"envelope missing field(s): {', '.join(missing)}")
    extra = [k for k in raw if k not in ENVELOPE_FIELDS]
    if extra:
        raise PoisonMessage(f"envelope has unexpected field(s): {', '.join(extra)}")
    checks = {
        "message_id": lambda v: isinstance(v, str) and _UUID.match(v),
        "message_type": lambda v: isinstance(v, str) and _MESSAGE_TYPE.match(v),
        "schema_version": lambda v: (
            isinstance(v, int) and not isinstance(v, bool) and v in SUPPORTED_SCHEMA_VERSIONS
        ),
        "occurred_at": lambda v: isinstance(v, str) and _UTC.match(v),
        "producer": lambda v: isinstance(v, str) and _PRODUCER.match(v),
        "payload": lambda v: isinstance(v, dict),
    }
    for field, ok in checks.items():
        if not ok(raw[field]):
            raise PoisonMessage(f"bad {field}: {raw[field]!r}")


def _errors(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()
    )


def parse(body: bytes, correlation_id: str | None = None) -> Message:
    try:
        raw = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PoisonMessage(f"body is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise PoisonMessage("envelope is not a JSON object")
    _check_envelope(raw)
    model = PAYLOAD_TYPES.get(raw["message_type"])
    payload = None
    if model is not None:
        try:
            payload = model.model_validate(raw["payload"])
        except ValidationError as exc:
            raise PoisonMessage(f"invalid {raw['message_type']} payload: {_errors(exc)}") from exc
    return Message(
        message_id=raw["message_id"],
        message_type=raw["message_type"],
        payload=payload,
        correlation_id=correlation_id or None,
    )
