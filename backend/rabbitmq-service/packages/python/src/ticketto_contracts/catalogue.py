"""The message catalogue and contracts bundled with this package.

Everything comes from data/, a copy of the bundle built by rabbitmq-service's
tools/build_contracts_bundle.py, so this module knows no naming rules of its own.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

_DATA = resources.files("ticketto_contracts") / "data"


@dataclass(frozen=True)
class Message:  # pylint: disable=too-many-instance-attributes  # mirrors catalogue.json
    name: str
    publisher: str
    exchange: str
    routing_key: str
    status: str
    consumers: tuple[str, ...]
    queues: tuple[str, ...]
    has_schema: bool
    has_example: bool


def _read(relative: str) -> Any:
    return json.loads((_DATA / relative).read_text(encoding="utf-8"))


@cache
def _catalogue() -> dict[str, Any]:
    if not (_DATA / "catalogue.json").is_file():
        raise RuntimeError(
            "ticketto_contracts/data is missing: build the bundle into it first (python -m "
            "tools.build_contracts_bundle --out packages/python/src/ticketto_contracts/data)"
        )
    return _read("catalogue.json")


def version() -> str:
    return _catalogue()["version"]


def services() -> list[str]:
    return sorted(_catalogue()["services"])


def messages() -> list[str]:
    return list(_catalogue()["messages"])


def message(name: str) -> Message | None:
    """The catalogued message called `name`, or None if there is none."""
    entry = _catalogue()["messages"].get(name)
    if entry is None:
        return None
    return Message(
        name=name,
        publisher=entry["publisher"],
        exchange=entry["exchange"],
        routing_key=entry["routing_key"],
        status=entry["status"],
        consumers=tuple(entry["consumers"]),
        queues=tuple(entry["queues"]),
        has_schema=entry["schema"] is not None,
        has_example=entry["example"] is not None,
    )


def _service(service: str) -> dict[str, Any]:
    try:
        return _catalogue()["services"][service]
    except KeyError:
        raise ValueError(
            f"{service!r} is not in the catalogue; known: {', '.join(services())}"
        ) from None


def published_by(service: str) -> list[str]:
    return list(_service(service)["publishes"])


def consumed_by(service: str) -> list[str]:
    return list(_service(service)["consumes"])


def queues_for(service: str) -> list[str]:
    """The queues `service` consumes from: `<service>.<Message>` for each message it consumes."""
    return list(_service(service)["queues"])


def exchange_for(service: str) -> str | None:
    """The exchange `service` publishes to, or None if it publishes nothing."""
    return _service(service)["exchange"]


def requirements(service: str) -> dict[str, list[str]]:
    """The payload fields `service` reads, per message it consumes."""
    return copy.deepcopy(_catalogue()["requirements"].get(service, {}))


@cache
def _schema(relative: str) -> dict[str, Any]:
    return _read(relative)


def envelope_schema() -> dict[str, Any]:
    return copy.deepcopy(_schema(_catalogue()["envelope_schema"]))


def payload_schema(name: str) -> dict[str, Any] | None:
    entry = _catalogue()["messages"].get(name)
    if entry is None or entry["schema"] is None:
        return None
    return copy.deepcopy(_schema(entry["schema"]))


def example(name: str) -> dict[str, Any]:
    """A copy of the message's example body."""
    entry = _catalogue()["messages"].get(name)
    if entry is None or entry["example"] is None:
        raise ValueError(f"{name!r} has no example in contracts {version()}")
    return copy.deepcopy(_schema(entry["example"]))
