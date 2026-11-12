"""Message envelope and the payloads Queue consumes (see the contracts doc).

parse_message turns raw AMQP bytes into a typed message, or raises PoisonMessage
for anything that can never succeed (bad JSON, unknown type, wrong schema
version, invalid fields). Poison is dead-lettered at once, never retried.
Unknown extra fields are ignored, so producers can add fields without a new
schema_version.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from app.core.timefmt import ensure_utc

SUPPORTED_SCHEMA_VERSION = 1


class PoisonMessage(ValueError):
    """A message that can never be processed; dead-letter it immediately."""


def _canonical_uuid(value: str) -> str:
    return str(uuid.UUID(value))


Uuid = Annotated[str, AfterValidator(_canonical_uuid)]  # stored lowercase, hyphenated
UtcDatetime = Annotated[AwareDatetime, AfterValidator(ensure_utc)]  # naive is rejected


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _EventConfig(_Payload):
    event_id: Uuid
    event_version: int = Field(ge=1)
    total_capacity: int = Field(ge=0)
    queue_join_opens_at: UtcDatetime  # buyers can join the (unordered) pre-queue from here
    release_at: UtcDatetime
    # Sent as starts_at: the event's start time, which is also when ticket sales end.
    # Optional; None = the sale never ends.
    sale_ends_at: UtcDatetime | None = Field(default=None, validation_alias="starts_at")

    @model_validator(mode="after")
    def _check_order(self) -> _EventConfig:
        if self.queue_join_opens_at > self.release_at:
            raise ValueError("queue_join_opens_at must not be later than release_at")
        if self.sale_ends_at is not None and self.sale_ends_at <= self.release_at:
            raise ValueError("starts_at must be later than release_at")
        return self


class EventPublished(_EventConfig):
    pass


class EventUpdated(_EventConfig):
    """Always the full current values, not only the changed ones."""


class EventCancelled(_Payload):
    event_id: Uuid
    event_version: int = Field(ge=1)
    cancelled_at: UtcDatetime


class EventSoldOut(_Payload):
    event_id: Uuid
    sold_out_at: UtcDatetime


class OrderConfirmed(_Payload):
    order_id: Uuid
    admission_id: Uuid
    user_id: Uuid
    event_id: Uuid
    confirmed_at: UtcDatetime


Payload = EventPublished | EventUpdated | EventCancelled | EventSoldOut | OrderConfirmed

PAYLOAD_TYPES: dict[str, type[Payload]] = {
    "EventPublished": EventPublished,
    "EventUpdated": EventUpdated,
    "EventCancelled": EventCancelled,
    "EventSoldOut": EventSoldOut,
    "OrderConfirmed": OrderConfirmed,
}


class Envelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    message_id: Uuid
    message_type: str
    schema_version: int
    occurred_at: UtcDatetime
    producer: str
    payload: dict[str, Any]


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


def parse_message(body: bytes) -> Message:
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
        raise PoisonMessage(f"unknown message_type {envelope.message_type!r}")
    try:
        payload = model.model_validate(envelope.payload)
    except ValidationError as exc:
        raise PoisonMessage(f"invalid {envelope.message_type} payload: {_errors(exc)}") from exc
    return Message(envelope, payload)


def _errors(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()
    )
