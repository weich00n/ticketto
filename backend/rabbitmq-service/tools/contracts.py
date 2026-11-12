"""The message contracts in contracts/, and the reference page generated from them.

Sources (edited by hand):
  topology/messages.yaml                   which messages exist, who publishes and consumes them
  contracts/envelope.schema.json           the body every message is wrapped in
  contracts/<Message>.payload.schema.json  each payload; owned by the message's publisher
  contracts/examples/<Message>.json        a valid example of each message
  contracts/requirements/<consumer>.yaml   the payload fields each consumer reads

Output (committed, checked by CI through `python -m tools.generate_definitions --check`):
  docs/AMQP_Message_Contracts.md           one page with all of the above, by message and by service
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:  # only for annotations: generate_definitions imports this module
    from tools.generate_definitions import Catalogue, Message

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "contracts"
REQUIREMENTS_DIR = CONTRACTS / "requirements"
CONTRACTS_DOC = ROOT / "docs" / "AMQP_Message_Contracts.md"

UUID_PATTERN = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
UTC_PATTERN = "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d+)?Z$"


# ---------- loading ----------


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def schema_path(message: str) -> Path:
    return CONTRACTS / f"{message}.payload.schema.json"


def load_envelope() -> dict[str, Any]:
    return _load_json(CONTRACTS / "envelope.schema.json")


def load_schema(message: str) -> dict[str, Any] | None:
    """The message's payload schema, or None if it has none yet."""
    path = schema_path(message)
    return _load_json(path) if path.exists() else None


def load_example(message: str) -> dict[str, Any] | None:
    path = CONTRACTS / "examples" / f"{message}.json"
    return _load_json(path) if path.exists() else None


def load_requirements(directory: Path = REQUIREMENTS_DIR) -> dict[str, dict[str, list[str]]]:
    """{consumer: {message: [path, ...]}} from contracts/requirements/*.yaml."""
    return {
        path.stem: yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for path in sorted(directory.glob("*.yaml"))
    }


def version() -> str:
    return (CONTRACTS / "VERSION").read_text(encoding="utf-8").strip()


# ---------- consumer requirements ----------


def resolve(node: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """Follow local `#/$defs/...` references. Contracts don't reference other files."""
    while "$ref" in node:
        node = root["$defs"][node["$ref"].removeprefix("#/$defs/")]
    return node


def requirement_problem(schema: dict[str, Any], path: str) -> str | None:
    """Why `path` (`field`, `object.field`, `array[].field`) isn't guaranteed by `schema`,
    or None if every step of it is a required property."""
    node = schema
    for part in path.split("."):
        name = part.removesuffix("[]")
        node = resolve(node, schema)
        if name not in node.get("properties", {}):
            return f"{name!r} is not in the publisher's schema"
        if name not in node.get("required", []):
            return f"{name!r} is optional in the publisher's schema"
        node = node["properties"][name]
        if part.endswith("[]"):
            node = resolve(node, schema)
            if node.get("type") != "array" or "items" not in node:
                return f"{name!r} is not an array in the publisher's schema"
            node = node["items"]
    return None


# ---------- the generated page ----------


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _short(service: str) -> str:
    return service.removesuffix("-service")


def _anchor(message: str) -> str:
    return f"[`{message}`](#{message.lower()})"


def _type_label(node: dict[str, Any], root: dict[str, Any]) -> str:  # pylint: disable=too-many-return-statements  # a flat lookup
    ref = node.get("$ref", "")
    target = resolve(node, root)
    kinds = target.get("type")
    if isinstance(kinds, list) and "null" in kinds:
        kind = next(k for k in kinds if k != "null")
        return f"{_type_label({**target, 'type': kind}, root)} or null"
    pattern = target.get("pattern")
    if ref.endswith("/uuid") or pattern == UUID_PATTERN:
        return "UUID"
    if ref.endswith("/utc") or pattern == UTC_PATTERN:
        return "UTC timestamp"
    if "const" in target:
        return f"`{json.dumps(target['const'])}`"
    kind = target.get("type", "any")
    if kind == "array":
        items = resolve(target.get("items", {}), root)
        return f"array of {items.get('type', 'any')}"
    if kind == "integer" and "minimum" in target:
        return f"integer ≥ {target['minimum']}"
    if kind == "string" and pattern and "@" in pattern:
        return "email"
    return str(kind)


def _fields(
    node: dict[str, Any], root: dict[str, Any], prefix: str = ""
) -> list[tuple[str, dict[str, Any], bool]]:
    """(path, schema node, required) for every property, depth first, arrays as `name[].x`."""
    node = resolve(node, root)
    rows: list[tuple[str, dict[str, Any], bool]] = []
    required = set(node.get("required", []))
    for name, child in node.get("properties", {}).items():
        path = f"{prefix}{name}"
        rows.append((path, child, name in required))
        target = resolve(child, root)
        if target.get("type") == "array" and "properties" in resolve(target.get("items", {}), root):
            rows.extend(_fields(target["items"], root, f"{path}[]."))
        elif "properties" in target:
            rows.extend(_fields(target, root, f"{path}."))
    return rows


def _readers(path: str, message: str, requirements: dict[str, dict[str, list[str]]]) -> str:
    """Consumers that read `path`, or anything inside it."""
    names = sorted(
        _short(consumer)
        for consumer, messages in requirements.items()
        if any(
            p == path or p.startswith(f"{path}[].") or p.startswith(f"{path}.")
            for p in messages.get(message, [])
        )
    )
    return ", ".join(names)


def _field_table(
    schema: dict[str, Any], message: str | None, requirements: dict[str, dict[str, list[str]]]
) -> list[str]:
    head = "| Field | Type | Required | Description |"
    rule = "|---|---|---|---|"
    if message:
        head, rule = head + " Read by |", rule + "---|"
    lines = [head, rule]
    for path, node, required in _fields(schema, schema):
        description = node.get("description") or resolve(node, schema).get("description", "")
        row = (
            f"| `{path}` | {_type_label(node, schema)} | {'yes' if required else 'no'} "
            f"| {_cell(description)} |"
        )
        if message:
            row += f" {_readers(path, message, requirements)} |"
        lines.append(row)
    return lines


def _message_section(message: Message, requirements: dict[str, dict[str, list[str]]]) -> list[str]:
    lines = [
        f"### {message.name}",
        "",
        f"- **Publisher:** {message.publisher} ({message.status})",
        f"- **Exchange, routing key:** `{message.exchange}`, `{message.routing_key}`",
        "- **Consumer queues:** "
        + (
            ", ".join(f"`{c}.{message.name}`" for c in message.consumers)
            or "none: kept in `ticketto.unrouted`"
        ),
    ]
    schema = load_schema(message.name)
    if schema is None:
        lines += ["", "No payload schema yet. One is required once the message has a consumer."]
        return lines
    lines += ["", _cell(schema.get("description", "")), ""]
    lines += _field_table(schema, message.name, requirements)
    example = load_example(message.name)
    if example is not None:
        lines += ["", "Example:", "", "```json", json.dumps(example, indent=2), "```"]
    return lines


def _service_section(
    service: str, catalogue: Catalogue, requirements: dict[str, dict[str, list[str]]]
) -> list[str]:
    published = [m for m in catalogue.messages if m.publisher == service]
    consumed = [m for m in catalogue.messages if service in m.consumers]
    lines = [f"### {service}", "", "**Publishes:**", ""]
    if published:
        lines += [f"- {_anchor(m.name)}: `{m.exchange}`, `{m.routing_key}`" for m in published]
    else:
        lines.append("_None_")
    if not consumed:
        return [*lines, "", "**Consumes:**", "", "_None_"]
    lines += [
        "",
        f"**Consumes** (each from its queue `{service}.<Message>`), and the payload fields "
        "it reads:",
        "",
    ]
    for m in consumed:
        paths = requirements.get(service, {}).get(m.name)
        fields = ", ".join(f"`{p}`" for p in paths) if paths else "_not listed yet_"
        lines.append(f"- {_anchor(m.name)}: {fields}")
    return lines


def render_contracts_doc(catalogue: Catalogue) -> str:
    requirements = load_requirements()
    services = sorted(
        {m.publisher for m in catalogue.messages}
        | {c for m in catalogue.messages for c in m.consumers}
    )
    lines = [
        "# AMQP message contracts",
        "",
        "<!-- Generated from topology/messages.yaml and contracts/ by "
        "tools/generate_definitions.py. Do not edit. -->",
        "",
        f"Contract version **{version()}** ([changelog](../contracts/CHANGELOG.md)). "
        "Generated from [`topology/messages.yaml`](../topology/messages.yaml) and "
        "[`contracts/`](../contracts/); "
        "edit those, not this page. How to publish and consume: [MESSAGING.md](MESSAGING.md).",
        "",
        "- **Publishers own their payload schemas** (`contracts/<Message>.payload.schema.json`). "
        "Every field listed is sent; consumers must ignore fields they don't know.",
        "- **Consumers list the fields they read** (`contracts/requirements/<consumer>.yaml`). CI "
        "fails if a publisher's schema stops requiring any of them.",
        "",
        "## Messages",
        "",
        "| Message | Publisher | Exchange, routing key | Consumers |",
        "|---|---|---|---|",
    ]
    for m in catalogue.messages:
        consumers = ", ".join(_short(c) for c in m.consumers) or "_none_"
        lines.append(
            f"| {_anchor(m.name)} | {_short(m.publisher)} | `{m.exchange}`, `{m.routing_key}` "
            f"| {consumers} |"
        )
    lines += [
        "",
        "## Envelope",
        "",
        "Every message body, whatever its type. The message-specific fields are in `payload`.",
        "",
    ]
    lines += _field_table(load_envelope(), None, requirements)
    lines += ["", "## Payloads", ""]
    for m in catalogue.messages:
        lines += _message_section(m, requirements) + [""]
    lines += ["## By service", ""]
    for service in services:
        lines += _service_section(service, catalogue, requirements) + [""]
    return "\n".join(lines).rstrip("\n") + "\n"
