"""Parse and validate broker messages (rabbitmq-service contracts/envelope.schema.json).

The envelope has exactly six top-level fields. The correlation id is not in the body: it
travels as the AMQP correlation_id property. A message that can never be processed (bad
JSON, wrong shape, unknown schema_version, invalid payload) is poison: the consumer
dead-letters it instead of retrying. A message type this service doesn't use parses with
payload None and is acknowledged and ignored. Unknown payload fields are ignored.
"""

import json
import re
import uuid
from dataclasses import dataclass

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_MESSAGE_TYPE = re.compile(r"^[A-Z][A-Za-z]+$")
_OCCURRED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
_PRODUCER = re.compile(r"^[a-z]+(-[a-z]+)*-service$")

REQUIRED = ("message_id", "message_type", "schema_version", "occurred_at", "producer", "payload")
SUPPORTED_SCHEMA_VERSIONS = (1,)


class PoisonMessage(Exception):
    """The message can never be processed; dead-letter it."""


@dataclass(frozen=True)
class OrderConfirmed:
    user_id: str  # the buyer's Cognito sub (checkout-service uses X-User-Sub as user_id)
    event_id: str


@dataclass(frozen=True)
class EventCancelled:
    event_id: str


Payload = OrderConfirmed | EventCancelled


@dataclass(frozen=True)
class Message:
    message_id: str
    message_type: str
    payload: Payload | None  # None: a message type user-service doesn't use
    correlation_id: str | None = None


def _uuid(payload: dict, field: str) -> str:
    try:
        return str(uuid.UUID(str(payload[field])))
    except (KeyError, ValueError) as exc:
        raise PoisonMessage(f"payload.{field} must be a UUID") from exc


def _order_confirmed(payload: dict) -> OrderConfirmed:
    user_id = payload.get("user_id")
    if not isinstance(user_id, str) or not 0 < len(user_id.strip()) <= 64:
        raise PoisonMessage("payload.user_id must be a non-empty string")
    return OrderConfirmed(user_id=user_id.strip().lower(), event_id=_uuid(payload, "event_id"))


def _event_cancelled(payload: dict) -> EventCancelled:
    return EventCancelled(event_id=_uuid(payload, "event_id"))


PAYLOAD_PARSERS = {"OrderConfirmed": _order_confirmed, "EventCancelled": _event_cancelled}


def _check_envelope(raw: dict) -> None:
    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        raise PoisonMessage(f"envelope missing field(s): {', '.join(missing)}")
    extra = [k for k in raw if k not in REQUIRED]
    if extra:
        raise PoisonMessage(f"envelope has unexpected field(s): {', '.join(extra)}")
    checks = {
        "message_id": lambda v: isinstance(v, str) and _UUID.match(v),
        "message_type": lambda v: isinstance(v, str) and _MESSAGE_TYPE.match(v),
        "schema_version": lambda v: (
            isinstance(v, int) and not isinstance(v, bool) and v in SUPPORTED_SCHEMA_VERSIONS
        ),
        "occurred_at": lambda v: isinstance(v, str) and _OCCURRED_AT.match(v),
        "producer": lambda v: isinstance(v, str) and _PRODUCER.match(v),
        "payload": lambda v: isinstance(v, dict),
    }
    for field, ok in checks.items():
        if not ok(raw[field]):
            raise PoisonMessage(f"bad {field}: {raw[field]!r}")


def parse(body: bytes, correlation_id: str | None = None) -> Message:
    try:
        raw = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PoisonMessage(f"body is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise PoisonMessage("envelope is not a JSON object")
    _check_envelope(raw)
    parser = PAYLOAD_PARSERS.get(raw["message_type"])
    return Message(
        message_id=raw["message_id"],
        message_type=raw["message_type"],
        payload=parser(raw["payload"]) if parser else None,
        correlation_id=correlation_id or None,
    )
