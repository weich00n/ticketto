"""Parse and validate the broker's message envelope.

Mirrors rabbitmq-service's contracts/envelope.schema.json: exactly six top-level fields,
nothing else (additionalProperties: false). The correlation id is not part of the body;
it travels as the AMQP correlation_id property and is passed in beside the body.

A message that fails to parse, or that has a shape the handler cannot act on, is poison:
the caller dead-letters it (reject without requeue) so it is parked in the queue's DLQ
rather than retried forever.
"""

import json
import re
from dataclasses import dataclass

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_MESSAGE_TYPE = re.compile(r"^[A-Z][A-Za-z]+$")
_OCCURRED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
_PRODUCER = re.compile(r"^[a-z]+(-[a-z]+)*-service$")

REQUIRED = ("message_id", "message_type", "schema_version", "occurred_at", "producer", "payload")

# Consumers reject payload versions they don't know (envelope.schema.json).
SUPPORTED_SCHEMA_VERSIONS = (1,)


class PoisonMessage(Exception):
    """The envelope (or its payload) is not something we can process; dead-letter it."""


@dataclass(frozen=True)
class Envelope:
    message_id: str
    message_type: str
    schema_version: int
    occurred_at: str
    producer: str
    payload: dict
    # From the AMQP property, not the body; None when the publisher did not set one.
    correlation_id: str | None = None


def parse(body: bytes, correlation_id: str | None = None) -> Envelope:
    try:
        raw = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PoisonMessage(f"body is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise PoisonMessage("envelope is not a JSON object")

    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        raise PoisonMessage(f"envelope missing field(s): {', '.join(missing)}")
    extra = [k for k in raw if k not in REQUIRED]
    if extra:
        raise PoisonMessage(f"envelope has unexpected field(s): {', '.join(extra)}")

    message_id = raw["message_id"]
    message_type = raw["message_type"]
    schema_version = raw["schema_version"]
    occurred_at = raw["occurred_at"]
    producer = raw["producer"]
    if not isinstance(message_id, str) or not _UUID.match(message_id):
        raise PoisonMessage(f"bad message_id: {message_id!r}")
    if not isinstance(message_type, str) or not _MESSAGE_TYPE.match(message_type):
        raise PoisonMessage(f"bad message_type: {message_type!r}")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in SUPPORTED_SCHEMA_VERSIONS
    ):
        raise PoisonMessage(f"unsupported schema_version: {schema_version!r}")
    if not isinstance(occurred_at, str) or not _OCCURRED_AT.match(occurred_at):
        raise PoisonMessage(f"bad occurred_at: {occurred_at!r}")
    if not isinstance(producer, str) or not _PRODUCER.match(producer):
        raise PoisonMessage(f"bad producer: {producer!r}")
    if not isinstance(raw["payload"], dict):
        raise PoisonMessage("payload is not a JSON object")

    return Envelope(
        message_id=message_id,
        message_type=message_type,
        schema_version=schema_version,
        occurred_at=occurred_at,
        producer=producer,
        payload=raw["payload"],
        correlation_id=correlation_id or None,
    )
