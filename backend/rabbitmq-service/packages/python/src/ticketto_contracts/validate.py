"""Check one publish against the contracts.

The problem codes and rules are shared with the Java package and pinned by
packages/conformance/cases.json in rabbitmq-service: change both together.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from ticketto_contracts import catalogue

PROPERTY_NAMES = ("content_type", "delivery_mode", "message_id", "type", "correlation_id")


@dataclass(frozen=True)
class PublishedMessage:
    """What a publisher handed to its AMQP client for one message."""

    exchange: str
    routing_key: str
    body: bytes
    properties: Mapping[str, Any] = field(default_factory=dict)
    mandatory: bool = False
    confirmed: bool = False  # publisher confirms were on for the channel


@dataclass(frozen=True)
class Problem:
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


class ContractError(AssertionError):
    """A publish or a consumer breaks the contract. Lists every problem, not just the first."""

    def __init__(self, subject: str, problems: list[Problem]):
        self.problems = problems
        lines = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"contracts {catalogue.version()} broken by {subject}:\n{lines}")


def _schema_errors(schema: dict[str, Any], value: Any) -> str:
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.path))
    return "; ".join(f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in errors)


def _property_problems(published: PublishedMessage) -> list[Problem]:
    props = published.properties
    problems = []
    if props.get("content_type") != "application/json":
        problems.append(
            Problem(
                "content_type",
                f"content_type is {props.get('content_type')!r}, not 'application/json'",
            )
        )
    if props.get("delivery_mode") != 2:
        problems.append(
            Problem(
                "delivery_mode",
                f"delivery_mode is {props.get('delivery_mode')!r}, not 2 (persistent)",
            )
        )
    for name in ("message_id", "type", "correlation_id"):
        if not props.get(name):
            problems.append(Problem(name, f"the {name} property is missing"))
    if not published.mandatory:
        problems.append(Problem("mandatory", "published without mandatory=True"))
    if not published.confirmed:
        problems.append(
            Problem("confirms", "publisher confirms were not on (channel.confirm_delivery())")
        )
    return problems


def _parse_body(body: bytes | str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _routing_problems(
    service: str, published: PublishedMessage, entry: catalogue.Message
) -> list[Problem]:
    problems = []
    if service != entry.publisher:
        problems.append(
            Problem(
                "not_publisher", f"{entry.name} is published by {entry.publisher}, not {service}"
            )
        )
    if published.exchange != entry.exchange:
        problems.append(
            Problem("exchange", f"published to {published.exchange!r}, not {entry.exchange!r}")
        )
    if published.routing_key != entry.routing_key:
        problems.append(
            Problem(
                "routing_key", f"routing key {published.routing_key!r}, not {entry.routing_key!r}"
            )
        )
    return problems


def _body_problems(
    service: str, published: PublishedMessage, body: dict[str, Any], name: str
) -> list[Problem]:
    props = published.properties
    problems = []
    if errors := _schema_errors(catalogue.envelope_schema(), body):
        problems.append(Problem("envelope", errors))
    if "producer" in body and body["producer"] != service:
        problems.append(
            Problem("producer", f"envelope producer is {body['producer']!r}, not {service!r}")
        )
    if props.get("type") and "message_type" in body and body["message_type"] != props["type"]:
        problems.append(
            Problem(
                "message_type_mismatch",
                f"envelope message_type {body['message_type']!r} differs from the type "
                f"property {props['type']!r}",
            )
        )
    if (
        props.get("message_id")
        and "message_id" in body
        and body["message_id"] != props["message_id"]
    ):
        problems.append(
            Problem(
                "message_id_mismatch",
                f"envelope message_id {body['message_id']!r} differs from the message_id property "
                f"{props['message_id']!r}",
            )
        )
    schema = catalogue.payload_schema(name)
    if schema is not None and isinstance(body.get("payload"), dict):
        if errors := _schema_errors(schema, body["payload"]):
            problems.append(Problem("payload", errors))
    return problems


def check_publish(service: str, published: PublishedMessage) -> list[Problem]:
    """Every way `published` breaks the contracts when sent by `service`; [] if it doesn't.

    The publish is checked as the message named by its `type` property, or by the envelope's
    message_type if `type` is missing.
    """
    problems = _property_problems(published)
    body = _parse_body(published.body)
    if body is None:
        problems.append(Problem("body_json", "the body is not a JSON object"))
    name = published.properties.get("type") or (body or {}).get("message_type")
    entry = catalogue.message(name) if isinstance(name, str) else None
    if entry is None:
        problems.append(Problem("unknown_message", f"{name!r} is not a message in the catalogue"))
    else:
        problems += _routing_problems(service, published, entry)
    if body is not None:
        problems += _body_problems(service, published, body, entry.name if entry else "")
    return problems


def assert_valid_publish(service: str, published: PublishedMessage) -> None:
    """Raise ContractError listing every problem with `published`."""
    if problems := check_publish(service, published):
        name = published.properties.get("type") or "a message"
        raise ContractError(f"{service} publishing {name}", problems)


def properties_dict(properties: Any) -> dict[str, Any]:
    """The checked AMQP properties of a pika BasicProperties (or any object or mapping with those
    attributes) as a plain dict. Enums such as pika.DeliveryMode become their values."""
    if properties is None:
        return {}
    if isinstance(properties, Mapping):
        source = dict(properties)
    else:
        source = {name: getattr(properties, name, None) for name in PROPERTY_NAMES}
    return {
        name: getattr(source.get(name), "value", source.get(name))
        for name in PROPERTY_NAMES
        if source.get(name) is not None
    }
