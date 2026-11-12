"""Message bodies for consumer tests, generated from each message's example.

minimal       only the fields the publisher's schema requires: what a consumer can count on
example       the committed example
extra-fields  the example plus an unknown field in every payload object: consumers must
              ignore fields they don't know
invalid       the example without one required payload field: consumers must reject it
              without requeueing
"""

from __future__ import annotations

import json
from typing import Any

from ticketto_contracts import catalogue

CASES = ("minimal", "example", "extra-fields", "invalid")
UNKNOWN_FIELD = "x_contract_test_unknown_field"


def _resolve(node: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        node = root["$defs"][node["$ref"].removeprefix("#/$defs/")]
    return node


def strip_optional(value: Any, node: dict[str, Any], root: dict[str, Any]) -> Any:
    """`value` with every property its schema doesn't require removed, recursively."""
    node = _resolve(node, root)
    if isinstance(value, dict) and "properties" in node:
        required = set(node.get("required", []))
        return {
            k: strip_optional(v, node["properties"].get(k, {}), root)
            for k, v in value.items()
            if k in required
        }
    if isinstance(value, list) and "items" in node:
        return [strip_optional(item, node["items"], root) for item in value]
    return value


def _add_unknown_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {**{k: _add_unknown_fields(v) for k, v in value.items()}, UNKNOWN_FIELD: "ignore me"}
    if isinstance(value, list):
        return [_add_unknown_fields(item) for item in value]
    return value


def example(name: str) -> dict[str, Any]:
    return catalogue.example(name)


def minimal(name: str) -> dict[str, Any]:
    body = catalogue.example(name)
    schema = catalogue.payload_schema(name)
    if schema is not None:
        body["payload"] = strip_optional(body["payload"], schema, schema)
    return body


def extra_fields(name: str) -> dict[str, Any]:
    body = catalogue.example(name)
    body["payload"] = _add_unknown_fields(body["payload"])
    return body


def invalid(name: str) -> dict[str, Any]:
    body = catalogue.example(name)
    schema = catalogue.payload_schema(name)
    if not schema or not schema.get("required"):
        raise ValueError(f"{name} has no required payload field to remove")
    del body["payload"][schema["required"][0]]
    return body


_BUILDERS = {
    "minimal": minimal,
    "example": example,
    "extra-fields": extra_fields,
    "invalid": invalid,
}


def body(name: str, case: str = "minimal") -> dict[str, Any]:
    try:
        return _BUILDERS[case](name)
    except KeyError:
        raise ValueError(f"unknown body case {case!r}; one of {', '.join(CASES)}") from None


def delivery(name: str, case: str = "minimal") -> tuple[bytes, dict[str, Any]]:
    """(body bytes, AMQP properties) as a well-behaved publisher would deliver the message."""
    built = body(name, case)
    return json.dumps(built).encode(), {
        "content_type": "application/json",
        "delivery_mode": 2,
        "message_id": built["message_id"],
        "type": name,
        "correlation_id": "corr-contract-test",
    }
