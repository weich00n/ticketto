"""Message contracts are valid JSON Schema, belong to catalogued messages, and their
examples validate.

The publisher owns a message's payload schema. Each consumer lists the payload fields it reads in
contracts/requirements/<consumer>.yaml, and every one of them must be required in the
publisher's schema, so a publisher can't drop or stop always sending a field a consumer needs.
"""

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tools import generate_definitions as gen
from tools.contracts import (
    CONTRACTS,
    UTC_PATTERN,
    _type_label,
    load_requirements,
    requirement_problem,
)

ENVELOPE = json.loads((CONTRACTS / "envelope.schema.json").read_text(encoding="utf-8"))
PAYLOAD_SCHEMAS = sorted(CONTRACTS.glob("*.payload.schema.json"))
EXAMPLES = sorted((CONTRACTS / "examples").glob("*.json"))
CATALOGUE = gen.load_catalogue()
CATALOGUED = {m.name for m in CATALOGUE.messages}

# (consumer, message) pairs in messages.yaml whose consumer hasn't listed its fields yet. May only
# shrink: an entry that has since been added to the consumer's file fails the test.
PENDING_REQUIREMENTS: set[tuple[str, str]] = set()


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _message_name(path: Path) -> str:
    return path.name.split(".")[0]


REQUIREMENTS = load_requirements()
CONSUMED = {(c, m.name) for m in CATALOGUE.messages for c in m.consumers}
REQUIREMENT_CASES = [
    (consumer, message, path)
    for consumer, messages in REQUIREMENTS.items()
    for message, paths in messages.items()
    for path in paths
]


@pytest.mark.parametrize("path", [CONTRACTS / "envelope.schema.json", *PAYLOAD_SCHEMAS], ids=str)
def test_schemas_are_valid(path):
    Draft202012Validator.check_schema(_load(path))


@pytest.mark.parametrize("path", PAYLOAD_SCHEMAS, ids=lambda p: p.name)
def test_every_contract_is_for_a_catalogued_message_and_has_an_example(path):
    name = _message_name(path)
    assert name in CATALOGUED
    assert (CONTRACTS / "examples" / f"{name}.json").exists()


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_examples_validate(path):
    example = _load(path)
    name = _message_name(path)
    Draft202012Validator(ENVELOPE).validate(example)
    assert example["message_type"] == name
    assert example["producer"] == CATALOGUE.message(name).publisher
    Draft202012Validator(_load(CONTRACTS / f"{name}.payload.schema.json")).validate(
        example["payload"]
    )


def test_envelope_rejects_the_old_flat_format():
    # event-service's pre-0002 format: flat body, no envelope fields.
    flat = {
        "message_id": "3d6f0c8e-6a3b-4f7d-9a51-0b2c1e7d9f40",
        "event_type": "EventPublished",
        "occurred_at": "2026-10-06T10:05:00Z",
        "event_id": "a14ec682-62f5-46db-b94a-a8d95435e274",
    }
    assert list(Draft202012Validator(ENVELOPE).iter_errors(flat))


def test_envelope_rejects_the_old_event_type_and_data_wrapper():
    # What contracts/ described before the teams settled on message_type/producer/payload.
    old = {
        "message_id": "3d6f0c8e-6a3b-4f7d-9a51-0b2c1e7d9f40",
        "event_type": "EventPublished",
        "schema_version": 1,
        "occurred_at": "2026-10-06T10:05:00Z",
        "correlation_id": "6f1c2a90-3b1e-4c55-9d1a-2f0e8a7b4c11",
        "data": {},
    }
    assert list(Draft202012Validator(ENVELOPE).iter_errors(old))


# ---------- every consumed message has a contract ----------


@pytest.mark.parametrize(
    "message", [m for m in CATALOGUE.messages if m.consumers], ids=lambda m: m.name
)
def test_every_consumed_message_has_a_payload_schema(message):
    assert (CONTRACTS / f"{message.name}.payload.schema.json").exists()


# ---------- consumer requirements ----------


@pytest.mark.parametrize("consumer", sorted(REQUIREMENTS))
def test_requirements_are_only_for_messages_the_consumer_consumes(consumer):
    listed = {(consumer, message) for message in REQUIREMENTS[consumer]}
    assert listed <= CONSUMED, f"not consumers in messages.yaml: {sorted(listed - CONSUMED)}"


@pytest.mark.parametrize("consumer", sorted(REQUIREMENTS))
def test_requirements_are_unique_lists_of_paths(consumer):
    for message, paths in REQUIREMENTS[consumer].items():
        assert isinstance(paths, list) and paths, f"{message}: expected a non-empty list"
        assert all(isinstance(p, str) for p in paths), message
        assert len(paths) == len(set(paths)), f"{message}: duplicate paths"


@pytest.mark.parametrize(
    ("node", "label"),
    [
        ({"type": "string"}, "string"),
        ({"type": ["string", "null"]}, "string or null"),
        ({"type": ["string", "null"], "pattern": UTC_PATTERN}, "UTC timestamp or null"),
        ({"type": ["string", "null"], "pattern": "^[^@\\s]+@[^@\\s]+$"}, "email or null"),
    ],
)
def test_reference_page_says_which_fields_can_be_null(node, label):
    assert _type_label(node, {}) == label


def _ticket_issued_errors(**changes) -> list:
    schema = _load(CONTRACTS / "TicketIssued.payload.schema.json")
    payload = {**_load(CONTRACTS / "examples" / "TicketIssued.json")["payload"], **changes}
    return list(Draft202012Validator(schema).iter_errors(payload))


@pytest.mark.parametrize(
    "field", ["delivery_email", "title", "venue", "starts_at", "category_name"]
)
def test_ticket_issued_field_can_be_null(field):
    assert not _ticket_issued_errors(**{field: None})


@pytest.mark.parametrize("field", ["order_id", "user_id", "event_id", "tickets"])
def test_ticket_issued_field_cant_be_null(field):
    assert _ticket_issued_errors(**{field: None})


TICKET = _load(CONTRACTS / "examples" / "TicketIssued.json")["payload"]["tickets"][0]


@pytest.mark.parametrize(
    "tickets",
    [
        pytest.param([], id="no tickets"),
        pytest.param([TICKET] * 6, id="more than an order can have"),
        pytest.param([{k: v for k, v in TICKET.items() if k != "qr_payload"}], id="no qr_payload"),
        pytest.param([{**TICKET, "qr_payload": "not-a-qr-code"}], id="malformed qr_payload"),
    ],
)
def test_ticket_issued_rejects_bad_tickets(tickets):
    assert _ticket_issued_errors(tickets=tickets)


def test_every_consumer_lists_the_fields_it_reads():
    listed = {(c, m) for c, messages in REQUIREMENTS.items() for m in messages}
    missing = CONSUMED - listed - PENDING_REQUIREMENTS
    assert not missing, f"add these to contracts/requirements/: {sorted(missing)}"


def test_pending_requirements_are_still_pending():
    listed = {(c, m) for c, messages in REQUIREMENTS.items() for m in messages}
    assert not PENDING_REQUIREMENTS & listed, "listed now: remove from PENDING_REQUIREMENTS"
    assert PENDING_REQUIREMENTS <= CONSUMED, "no longer consumed: remove from PENDING_REQUIREMENTS"


@pytest.mark.parametrize(("consumer", "message", "path"), REQUIREMENT_CASES, ids=str)
def test_every_field_a_consumer_reads_is_required_by_the_publisher(consumer, message, path):
    schema = _load(CONTRACTS / f"{message}.payload.schema.json")
    problem = requirement_problem(schema, path)
    assert problem is None, f"{consumer} reads {message}.payload.{path}, but {problem}"


# ---------- the requirement check itself ----------

SAMPLE = {
    "type": "object",
    "required": ["id", "items"],
    "properties": {
        "id": {"$ref": "#/$defs/uuid"},
        "note": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}, "size": {"type": "integer"}},
            },
        },
    },
    "$defs": {"uuid": {"type": "string"}},
}


@pytest.mark.parametrize(
    ("path", "problem"),
    [
        ("id", None),
        ("items[].name", None),
        ("missing", "'missing' is not in the publisher's schema"),
        ("note", "'note' is optional in the publisher's schema"),
        ("items[].size", "'size' is optional in the publisher's schema"),
        ("id[].x", "'id' is not an array in the publisher's schema"),
        ("items.name", "'name' is not in the publisher's schema"),
    ],
)
def test_requirement_problem(path, problem):
    assert requirement_problem(SAMPLE, path) == problem


# ---------- version and changelog ----------


def test_contract_version_is_semver_and_has_a_changelog_entry():
    version = (CONTRACTS / "VERSION").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"not MAJOR.MINOR.PATCH: {version!r}"
    changelog = (CONTRACTS / "CHANGELOG.md").read_text(encoding="utf-8")
    entries = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", changelog, flags=re.MULTILINE)
    assert entries, "CHANGELOG.md has no version entries"
    assert entries[0] == version, f"newest CHANGELOG.md entry is {entries[0]}, VERSION is {version}"
