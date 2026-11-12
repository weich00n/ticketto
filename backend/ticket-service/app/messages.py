"""Message envelope and the payloads Ticket consumes (see docs/API.md).

parse_message turns raw AMQP bytes into a typed message, returns None for a message type
Ticket does not use (acknowledge and ignore), or raises PoisonMessage for anything that can
never succeed (bad JSON, wrong schema version, missing or invalid required fields). Poison is
dead-lettered at once, never retried. Unknown extra fields are ignored.

Event and Queue use `message_type` and `payload`; the Technical Contract tab uses
`event_type` and `data`. Both spellings are accepted.
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
    ValidationError,
    model_validator,
)

SUPPORTED_SCHEMA_VERSION = 1
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")


class PoisonMessage(ValueError):
    """A message that can never be processed; dead-letter it immediately."""


def _canonical_uuid(value: str) -> str:
    return str(uuid.UUID(str(value)))


def parse_utc(value: Any) -> datetime | None:
    """Best-effort ISO 8601 parse. Optional timestamps never make a message poison."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).replace(microsecond=0)


def _optional_email(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if len(value) <= 320 and _EMAIL.match(value) else None


def _optional_uuid(value: Any) -> str | None:
    """For IDs Ticket only records: a missing or malformed one never makes a message poison."""
    if value in (None, ""):
        return None
    try:
        return _canonical_uuid(value)
    except ValueError:
        return None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdecimal() and value.strip().isascii():
        return int(value.strip())
    return None


def _optional_text(limit: int):
    def clean(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text[:limit] or None

    return clean


Uuid = Annotated[str, AfterValidator(_canonical_uuid)]
OptionalUuid = Annotated[str | None, BeforeValidator(_optional_uuid)]
OptionalInt = Annotated[int | None, BeforeValidator(_optional_int)]
OptionalTime = Annotated[datetime | None, BeforeValidator(parse_utc)]
UserId = Annotated[str, Field(min_length=1, max_length=64)]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class OrderConfirmed(_Payload):
    order_id: Uuid
    # Queue and Reservation need these; Ticket only records them, so they are optional.
    hold_id: OptionalUuid = None
    admission_id: OptionalUuid = None
    user_id: UserId
    event_id: Uuid
    category_id: Uuid
    quantity: int = Field(ge=1, le=5)
    delivery_email: Annotated[str | None, BeforeValidator(_optional_email)] = None
    confirmed_at: OptionalTime = None

    @model_validator(mode="before")
    @classmethod
    def _email_aliases(cls, data: Any) -> Any:
        # Tolerate the field names used by the early Checkout prototype.
        if isinstance(data, dict) and "delivery_email" not in data:
            for alias in ("customer_email", "email"):
                if alias in data:
                    return {**data, "delivery_email": data[alias]}
        return data


class RefundCompleted(_Payload):
    """A full refund: Checkout sends it only once the booking is cancelled and the money is
    back, so every ticket of the order becomes invalid."""

    order_id: Uuid
    event_id: Uuid
    refund_id: Annotated[str | None, BeforeValidator(_optional_text(64))] = None
    refund_amount: OptionalInt = None
    currency: Annotated[str | None, BeforeValidator(_optional_text(3))] = None
    completed_at: OptionalTime = None

    @model_validator(mode="before")
    @classmethod
    def _amount_alias(cls, data: Any) -> Any:
        # Checkout sends the refunded amount (in cents) as `amount`.
        if isinstance(data, dict) and "refund_amount" not in data and "amount" in data:
            return {**data, "refund_amount": data["amount"]}
        return data


class EventCancelled(_Payload):
    event_id: Uuid
    event_version: int | None = Field(default=None, ge=1)
    cancellation_id: Annotated[str | None, BeforeValidator(_optional_text(64))] = None
    cancelled_at: OptionalTime = None
    reason: Annotated[str | None, BeforeValidator(_optional_text(500))] = None


class CategoryRef(_Payload):
    category_id: Uuid
    name: Annotated[str, Field(min_length=1, max_length=80)]


def _valid_categories(value: Any) -> list[CategoryRef]:
    """The snapshot is only for display: skip a malformed category rather than reject all."""
    if not isinstance(value, list):
        return []
    categories = []
    for item in value:
        try:
            categories.append(CategoryRef.model_validate(item))
        except ValidationError:
            continue
    return categories


class EventSnapshot(_Payload):
    """EventPublished and EventUpdated: only the fields shown on tickets."""

    event_id: Uuid
    event_version: int | None = Field(default=None, ge=1)
    title: Annotated[str | None, BeforeValidator(_optional_text(160))] = None
    venue: Annotated[str | None, BeforeValidator(_optional_text(200))] = None
    starts_at: OptionalTime = None
    ticket_categories: Annotated[list[CategoryRef], BeforeValidator(_valid_categories)] = Field(
        default_factory=list
    )


Payload = OrderConfirmed | RefundCompleted | EventCancelled | EventSnapshot

PAYLOAD_TYPES: dict[str, type[Payload]] = {
    "OrderConfirmed": OrderConfirmed,
    "RefundCompleted": RefundCompleted,
    "EventCancelled": EventCancelled,
    "EventPublished": EventSnapshot,
    "EventUpdated": EventSnapshot,
}


class Envelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    message_id: Annotated[str, Field(min_length=1, max_length=64)]
    message_type: Annotated[str, Field(min_length=1, max_length=64)]
    schema_version: int = SUPPORTED_SCHEMA_VERSION
    occurred_at: OptionalTime = None
    producer: str | None = None
    correlation_id: str | None = None
    payload: dict[str, Any]

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            if "message_type" not in data and "event_type" in data:
                data["message_type"] = data["event_type"]
            if "payload" not in data and "data" in data:
                data["payload"] = data["data"]
        return data


@dataclass(frozen=True, slots=True)
class Message:
    envelope: Envelope
    payload: Payload

    @property
    def message_id(self) -> str:
        return self.envelope.message_id

    @property
    def message_type(self) -> str:
        return self.envelope.message_type


def parse_message(body: bytes) -> Message | None:
    try:
        raw = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PoisonMessage("body is not JSON") from exc
    if not isinstance(raw, dict):
        raise PoisonMessage("body is not a JSON object")
    try:
        envelope = Envelope.model_validate(raw)
    except ValidationError as exc:
        raise PoisonMessage(f"invalid envelope: {_errors(exc)}") from exc
    if envelope.schema_version != SUPPORTED_SCHEMA_VERSION:
        raise PoisonMessage(f"unsupported schema_version {envelope.schema_version}")
    model = PAYLOAD_TYPES.get(envelope.message_type)
    if model is None:
        return None
    try:
        payload = model.model_validate(envelope.payload)
    except ValidationError as exc:
        raise PoisonMessage(f"invalid {envelope.message_type} payload: {_errors(exc)}") from exc
    return Message(envelope, payload)


def _errors(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()
    )
