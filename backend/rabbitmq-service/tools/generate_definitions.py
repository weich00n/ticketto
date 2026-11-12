"""Expand topology/messages.yaml into the broker's RabbitMQ definitions.

Outputs (all committed, all checked by CI):
  topology/definitions.json  imported by the broker at every boot. It holds no users or
                             passwords: docker-entrypoint-ticketto.sh adds those at runtime.
  topology/queues.txt        every queue name, read by the in-image `ticketto-check`.
  topology/service-users.tsv one least-privilege user per service and its permissions, read
                             by docker-entrypoint-ticketto.sh (passwords come from the
                             environment, never from this file).
  docs/AMQP_Message_Contracts.md
                             the message contracts reference page, from this catalogue and
                             contracts/ (tools/contracts.py).

Usage:
  python -m tools.generate_definitions          # rewrite the outputs
  python -m tools.generate_definitions --check  # exit 1 if the committed outputs are stale
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tools import contracts

ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = ROOT / "topology" / "messages.yaml"
DEFINITIONS = ROOT / "topology" / "definitions.json"
QUEUE_LIST = ROOT / "topology" / "queues.txt"
SERVICE_USERS = ROOT / "topology" / "service-users.tsv"

# Each publisher has its own topic exchange, `<service without -service>.events`
# (event-service -> event.events). The two below are the broker's own.
DEAD_LETTER_EXCHANGE = "ticketto.dlx"
UNROUTED = "ticketto.unrouted"  # both the alternate exchange and the queue behind it

SERVICE_NAME = re.compile(r"^[a-z]+(-[a-z]+)*-service$")
MESSAGE_NAME = re.compile(r"^[A-Z][A-Za-z]+$")
# Dotted lower snake case, at least two parts: `event.published`, `event.sold_out`.
ROUTING_KEY = re.compile(r"^[a-z]+(_[a-z]+)*(\.[a-z]+(_[a-z]+)*)+$")
STATUSES = ("implemented", "planned", "proposed")

# Policy patterns. RabbitMQ matches them anywhere in the name with Erlang's `re`, which
# behaves like Python's re.search for these expressions.
CONSUMER_QUEUE_PATTERN = r"^[a-z]+(-[a-z]+)*-service\.[A-Za-z]+$"
HOLDING_QUEUE_PATTERN = r"\.dlq$|^ticketto\.unrouted$"
PUBLISHER_EXCHANGE_PATTERN = r"^[a-z]+(-[a-z]+)*\.events$"

GENERATED_NOTE = "Generated from topology/messages.yaml by tools/generate_definitions.py"


class CatalogueError(ValueError):
    """messages.yaml is malformed; the message says where."""


@dataclass(frozen=True)
class Message:
    name: str
    publisher: str
    routing_key: str
    status: str
    consumers: tuple[str, ...]

    @property
    def exchange(self) -> str:
        return publisher_exchange(self.publisher)


@dataclass(frozen=True)
class ConsumerQueue:
    name: str  # <consumer>.<Message>
    message: Message


@dataclass(frozen=True)
class Catalogue:
    vhost: str
    delivery_limit: int
    holding_max_length: int
    messages: tuple[Message, ...]

    def consumer_queues(self) -> list[ConsumerQueue]:
        """One queue for every consumer of every message."""
        return [ConsumerQueue(f"{c}.{m.name}", m) for m in self.messages for c in m.consumers]

    def exchanges(self) -> list[str]:
        """Every publisher's exchange, including publishers whose messages have no consumer."""
        return sorted({m.exchange for m in self.messages})

    def message(self, name: str) -> Message:
        return next(m for m in self.messages if m.name == name)


def publisher_exchange(service: str) -> str:
    return f"{service.removesuffix('-service')}.events"


# ---------- loading and validation ----------


class _UniqueKeyLoader(yaml.SafeLoader):  # pylint: disable=too-many-ancestors  # PyYAML's own
    """PyYAML silently keeps the last of two duplicate keys; a catalogue must not."""


def _construct_unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict:
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)
        if key in seen:
            raise CatalogueError(f"duplicate key {key!r} (line {key_node.start_mark.line + 1})")
        seen.add(key)
    return loader.construct_mapping(node)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _require_keys(where: str, value: Any, required: set[str], optional: set[str]) -> dict:
    if not isinstance(value, dict):
        raise CatalogueError(f"{where}: expected a mapping")
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise CatalogueError(f"{where}: missing {', '.join(sorted(missing))}")
    if unknown:
        raise CatalogueError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    return value


def _positive_int(where: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CatalogueError(f"{where}: expected a positive integer, got {value!r}")
    return value


def _service(where: str, value: Any) -> str:
    if not isinstance(value, str) or not SERVICE_NAME.match(value):
        raise CatalogueError(f"{where}: {value!r} is not a service name like 'event-service'")
    return value


def parse_catalogue(raw: Any) -> Catalogue:
    """Validate the parsed YAML and return it as a Catalogue."""
    top = _require_keys("messages.yaml", raw, {"broker", "messages"}, set())
    broker = _require_keys(
        "broker", top["broker"], {"vhost", "delivery_limit", "holding_max_length"}, set()
    )
    if not isinstance(broker["vhost"], str) or not broker["vhost"]:
        raise CatalogueError("broker.vhost: expected a non-empty string")
    if not isinstance(top["messages"], dict) or not top["messages"]:
        raise CatalogueError("messages: expected at least one message")

    messages = []
    routing_keys: dict[str, str] = {}  # routing key -> message; unique across all exchanges
    for name, body in top["messages"].items():
        where = f"messages.{name}"
        if not isinstance(name, str) or not MESSAGE_NAME.match(name):
            raise CatalogueError(f"{where}: message names are PascalCase, like 'EventPublished'")
        body = _require_keys(
            where, body, {"publisher", "routing_key", "status", "consumers"}, {"note"}
        )
        routing_key = body["routing_key"]
        if not isinstance(routing_key, str) or not ROUTING_KEY.match(routing_key):
            raise CatalogueError(
                f"{where}.routing_key: {routing_key!r} is not dotted lower case, "
                "like 'event.published'"
            )
        if routing_key in routing_keys:
            used_by = routing_keys[routing_key]
            raise CatalogueError(
                f"{where}.routing_key: {routing_key!r} is already used by {used_by}"
            )
        routing_keys[routing_key] = name
        if body["status"] not in STATUSES:
            raise CatalogueError(f"{where}.status: must be one of {', '.join(STATUSES)}")
        consumers = body["consumers"]
        if not isinstance(consumers, list):
            raise CatalogueError(f"{where}.consumers: expected a list (use [] for none yet)")
        consumers = [_service(f"{where}.consumers", c) for c in consumers]
        if len(set(consumers)) != len(consumers):
            raise CatalogueError(f"{where}.consumers: lists a service twice")
        messages.append(
            Message(
                name=name,
                publisher=_service(f"{where}.publisher", body["publisher"]),
                routing_key=routing_key,
                status=body["status"],
                consumers=tuple(consumers),
            )
        )

    return Catalogue(
        vhost=broker["vhost"],
        delivery_limit=_positive_int("broker.delivery_limit", broker["delivery_limit"]),
        holding_max_length=_positive_int("broker.holding_max_length", broker["holding_max_length"]),
        messages=tuple(messages),
    )


def load_catalogue(path: Path = CATALOGUE) -> Catalogue:
    with path.open(encoding="utf-8") as handle:
        return parse_catalogue(yaml.load(handle, Loader=_UniqueKeyLoader))  # noqa: S506


# ---------- building the definitions ----------


def build_definitions(catalogue: Catalogue) -> dict[str, Any]:
    """The RabbitMQ definitions document for this catalogue (no users or permissions)."""
    vhost = catalogue.vhost

    def exchange(name: str, kind: str) -> dict[str, Any]:
        # Publishers declare their own exchange with exactly these settings (topic, durable,
        # no arguments); any difference would make that declare fail with 406.
        return {
            "name": name,
            "vhost": vhost,
            "type": kind,
            "durable": True,
            "auto_delete": False,
            "internal": False,
            "arguments": {},
        }

    def queue(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": name,
            "vhost": vhost,
            "durable": True,
            "auto_delete": False,
            "arguments": {"x-queue-type": "quorum", **arguments},
        }

    def binding(source: str, destination: str, routing_key: str) -> dict[str, Any]:
        return {
            "source": source,
            "vhost": vhost,
            "destination": destination,
            "destination_type": "queue",
            "routing_key": routing_key,
            "arguments": {},
        }

    def policy(name: str, pattern: str, apply_to: str, definition: dict) -> dict[str, Any]:
        return {
            "vhost": vhost,
            "name": name,
            "pattern": pattern,
            "apply-to": apply_to,
            "priority": 0,
            "definition": definition,
        }

    queues = [queue(UNROUTED, {})]
    bindings = [binding(UNROUTED, UNROUTED, "")]
    for consumer_queue in catalogue.consumer_queues():
        name, message = consumer_queue.name, consumer_queue.message
        # Fixed structure lives in queue arguments: it never changes for a given queue.
        queues.append(
            queue(
                name,
                {
                    "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE,
                    "x-dead-letter-routing-key": name,
                    # Dead-lettering can't lose a message; it needs reject-publish overflow.
                    "x-dead-letter-strategy": "at-least-once",
                    "x-overflow": "reject-publish",
                },
            )
        )
        queues.append(queue(f"{name}.dlq", {}))
        bindings.append(binding(message.exchange, name, message.routing_key))
        bindings.append(binding(DEAD_LETTER_EXCHANGE, f"{name}.dlq", name))

    return {
        "vhosts": [
            {
                "name": vhost,
                "description": "Ticketto",
                "metadata": {
                    "description": "Ticketto",
                    "tags": [],
                    # A queue a client declares without a type is still durable and replicated.
                    "default_queue_type": "quorum",
                },
            }
        ],
        "exchanges": [
            *(exchange(name, "topic") for name in catalogue.exchanges()),
            exchange(DEAD_LETTER_EXCHANGE, "direct"),
            exchange(UNROUTED, "fanout"),
        ],
        "queues": sorted(queues, key=lambda q: q["name"]),
        "bindings": sorted(
            bindings, key=lambda b: (b["source"], b["routing_key"], b["destination"])
        ),
        # Tunables live in policies: editing one applies live, without recreating queues.
        "policies": [
            # Unroutable messages are kept instead of being returned to the publisher. A
            # policy, not an argument, so a publisher's argument-less declare still matches.
            policy(
                "events-ae",
                PUBLISHER_EXCHANGE_PATTERN,
                "exchanges",
                {"alternate-exchange": UNROUTED},
            ),
            # A message redelivered this many times without an ack goes to its DLQ.
            policy(
                "consumer-queues",
                CONSUMER_QUEUE_PATTERN,
                "queues",
                {"delivery-limit": catalogue.delivery_limit},
            ),
            # DLQs and ticketto.unrouted hold messages until a person deals with them.
            #  - max-length: disk safety cap. When full, new messages are refused (the
            #    publisher gets a nack), never silently dropped.
            #  - delivery-limit -1: quorum queues otherwise drop a message after 20 failed
            #    deliveries, and these queues have no dead-letter exchange to catch it.
            policy(
                "holding-cap",
                HOLDING_QUEUE_PATTERN,
                "queues",
                {
                    "max-length": catalogue.holding_max_length,
                    "overflow": "reject-publish",
                    "delivery-limit": -1,
                },
            ),
        ],
    }


# ---------- service users ----------

NOTHING = "^$"  # a permission regex that matches no resource name


@dataclass(frozen=True)
class ServiceUser:
    """A service's broker user. Each field is a regex that RabbitMQ matches against resource
    names (configure/write/read) or routing keys (topic_write, on topic_exchange)."""

    name: str
    configure: str  # declare/delete: only its own exchange, so its idempotent declare works
    write: str  # publish: only to its own exchange
    read: str  # consume/get: only its own consumer queues, not their DLQs
    topic_exchange: str | None  # None: publishes nothing
    topic_write: str  # publish: only the routing keys messages.yaml gives its messages


def _literal(name: str) -> str:
    """name as a regex. Service, exchange and routing-key names only contain letters, digits,
    '-', '_' and '.', and of those only '.' means something in a regex."""
    return name.replace(".", r"\.")


def _alternatives(names: list[str]) -> str:
    return "^(" + "|".join(_literal(n) for n in sorted(names)) + ")$"


def service_users(catalogue: Catalogue) -> list[ServiceUser]:
    """One user per service in the catalogue, as publisher, consumer or both."""
    services = sorted(
        {m.publisher for m in catalogue.messages}
        | {c for m in catalogue.messages for c in m.consumers}
    )
    users = []
    for service in services:
        published = [m for m in catalogue.messages if m.publisher == service]
        consumes = any(service in m.consumers for m in catalogue.messages)
        exchange = publisher_exchange(service) if published else None
        own_exchange = f"^{_literal(exchange)}$" if exchange else NOTHING
        users.append(
            ServiceUser(
                name=service,
                configure=own_exchange,
                write=own_exchange,
                read=rf"^{_literal(service)}\.[A-Za-z]+$" if consumes else NOTHING,
                topic_exchange=exchange,
                topic_write=_alternatives([m.routing_key for m in published])
                if published
                else NOTHING,
            )
        )
    return users


def _service_users_tsv(catalogue: Catalogue) -> str:
    # Fields are written JSON-escaped (a regex's backslash becomes two), because the entrypoint
    # pastes them into JSON strings and has no JSON tool to escape them itself.
    def field(value: str | None) -> str:
        return "-" if value is None else json.dumps(value)[1:-1]

    lines = [
        f"# {GENERATED_NOTE}. Do not edit.",
        "# Permission regexes, JSON-escaped. topic_exchange '-' = publishes nothing.",
        "# user\tconfigure\twrite\tread\ttopic_exchange\ttopic_write",
    ]
    for user in service_users(catalogue):
        lines.append(
            "\t".join(
                [
                    user.name,
                    field(user.configure),
                    field(user.write),
                    field(user.read),
                    field(user.topic_exchange),
                    field(user.topic_write),
                ]
            )
        )
    return "\n".join(lines) + "\n"


def queue_names(catalogue: Catalogue) -> list[str]:
    return [q["name"] for q in build_definitions(catalogue)["queues"]]


def render(catalogue: Catalogue) -> dict[Path, str]:
    """The exact text of every generated file."""
    queue_list = "".join(f"{name}\n" for name in queue_names(catalogue))
    return {
        DEFINITIONS: json.dumps(build_definitions(catalogue), indent=2) + "\n",
        QUEUE_LIST: f"# {GENERATED_NOTE}. Do not edit.\n{queue_list}",
        SERVICE_USERS: _service_users_tsv(catalogue),
        contracts.CONTRACTS_DOC: contracts.render_contracts_doc(catalogue),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="fail if the committed outputs are out of date"
    )
    args = parser.parse_args(argv)

    try:
        catalogue = load_catalogue()
    except CatalogueError as error:
        print(f"messages.yaml is invalid: {error}", file=sys.stderr)
        return 2

    outputs = render(catalogue)
    if args.check:
        stale = [
            path.relative_to(ROOT)
            for path, text in outputs.items()
            if not path.exists() or path.read_text(encoding="utf-8") != text
        ]
        if stale:
            names = ", ".join(str(p) for p in stale)
            print(
                f"Out of date: {names}. Run `python -m tools.generate_definitions` and commit.",
                file=sys.stderr,
            )
            return 1
        print("Generated topology is up to date.")
        return 0

    for path, text in outputs.items():
        path.write_text(text, encoding="utf-8")
    consumer_queues = len(catalogue.consumer_queues())
    print(
        f"{len(catalogue.messages)} messages on {len(catalogue.exchanges())} exchanges -> "
        f"{consumer_queues} consumer queues, "
        f"{consumer_queues} DLQs and {UNROUTED}; "
        f"{len(service_users(catalogue))} service users. Wrote "
        + ", ".join(str(p.relative_to(ROOT)) for p in outputs)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
