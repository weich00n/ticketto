"""The contracts bundle (tools/build_contracts_bundle.py) and the compatibility check
(tools/check_contract_compat.py): one test per rule in the versioning table of
docs/CONTRACT-TESTING-PLAN.md."""

import copy
import json
import shutil

import pytest

from tools import build_contracts_bundle as bundles
from tools import check_contract_compat as compat
from tools import generate_definitions as gen
from tools.contracts import CONTRACTS

CURRENT = bundles.collect()


@pytest.fixture()
def new():
    """A copy of today's bundle to change."""
    return copy.deepcopy(CURRENT)


def changes(new_bundle):
    return compat.diff(CURRENT, new_bundle)


def only(new_bundle):
    """The single change from CURRENT to new_bundle."""
    found = changes(new_bundle)
    assert len(found) == 1, [str(c) for c in found]
    return found[0]


# ---------- the bundle ----------


def test_bundle_has_every_message_service_schema_and_example():
    catalogue = gen.load_catalogue()
    assert list(CURRENT.messages) == [m.name for m in catalogue.messages]
    for m in catalogue.messages:
        entry = CURRENT.messages[m.name]
        assert entry["exchange"] == m.exchange
        assert entry["routing_key"] == m.routing_key
        assert entry["queues"] == [f"{c}.{m.name}" for c in m.consumers]
        assert (entry["schema"] is not None) == (
            CONTRACTS / f"{m.name}.payload.schema.json"
        ).exists()
    assert CURRENT.version == (CONTRACTS / "VERSION").read_text(encoding="utf-8").strip()


def test_bundle_services():
    services = CURRENT.catalogue["services"]
    assert services["reservation-service"]["exchange"] == "reservation.events"
    assert services["reservation-service"]["publishes"] == [
        "ReservationExpired",
        "RefundRequired",
        "EventSoldOut",
    ]
    assert services["user-service"]["exchange"] is None  # consumes only
    assert services["user-service"]["queues"] == [
        "user-service.EventCancelled",
        "user-service.OrderConfirmed",
    ]


def test_bundle_round_trips_and_is_deterministic(tmp_path):
    bundles.write_bundle(CURRENT, tmp_path / "a")
    bundles.write_bundle(bundles.load_bundle(tmp_path / "a"), tmp_path / "b")
    for path in sorted((tmp_path / "a").rglob("*")):
        if path.is_file():
            twin = tmp_path / "b" / path.relative_to(tmp_path / "a")
            assert twin.read_bytes() == path.read_bytes(), path.name
    assert compat.diff(CURRENT, bundles.load_bundle(tmp_path / "a")) == []


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_bundle_from_a_git_ref():
    at_head = bundles.collect_ref("HEAD")
    assert set(at_head.messages) >= {"EventPublished", "EventCancelled"}


# ---------- patch ----------


def test_description_change_is_patch(new):
    new.schemas["OrderConfirmed"]["properties"]["quantity"]["description"] = "How many tickets."
    assert only(new).level == "patch"


def test_example_change_is_patch(new):
    new.examples["OrderConfirmed"]["payload"]["quantity"] = 3
    change = only(new)
    assert change.level == "patch"
    assert change.services == {
        "checkout-service",
        "ticket-service",
        "queue-service",
        "user-service",
    }


def test_status_change_is_patch(new):
    new.messages["TicketIssued"]["status"] = "implemented"
    assert only(new).level == "patch"


# ---------- minor ----------


def test_new_optional_field_is_minor(new):
    new.schemas["OrderConfirmed"]["properties"]["promo_code"] = {"type": "string"}
    change = only(new)
    assert change.level == "minor"
    assert "optional field `promo_code` added" in change.what


def test_removing_a_field_no_consumer_reads_is_minor(new):
    # notification-service is RefundFailed's only consumer and doesn't read last_error.
    schema = new.schemas["RefundFailed"]
    del schema["properties"]["last_error"]
    schema["required"].remove("last_error")
    assert only(new).level == "minor"


def test_making_a_field_no_consumer_reads_optional_is_minor(new):
    new.schemas["RefundFailed"]["required"].remove("last_error")
    assert only(new).level == "minor"


def test_new_message_is_minor(new):
    new.messages["ReservationCreated"] = {
        "publisher": "reservation-service",
        "exchange": "reservation.events",
        "routing_key": "reservation.created",
        "status": "planned",
        "consumers": [],
        "queues": [],
        "schema": None,
        "example": None,
    }
    assert only(new).level == "minor"


def test_new_consumer_is_minor(new):
    new.messages["TicketIssued"]["consumers"].append("user-service")
    change = only(new)
    assert change.level == "minor"
    assert change.services == {"user-service"}


def test_new_schema_is_minor(new):
    del new.schemas["TicketIssued"]  # the base has no schema, today's bundle does
    [change] = compat.diff(new, CURRENT)
    assert change.level == "minor"


def test_consumer_reading_fewer_fields_is_minor(new):
    new.requirements["user-service"]["OrderConfirmed"].remove("event_id")
    change = only(new)
    assert change.level == "minor"
    assert change.services == {"user-service"}


# ---------- none ----------


def test_consumer_reading_a_new_field_needs_no_bump(new):
    new.requirements["user-service"]["OrderConfirmed"].append("order_id")
    assert only(new).level == "none"


# ---------- major ----------


def test_removing_a_field_a_consumer_reads_is_major_and_names_it(new):
    schema = new.schemas["OrderConfirmed"]
    del schema["properties"]["admission_id"]
    schema["required"].remove("admission_id")
    change = only(new)
    assert change.level == "major"
    assert "queue-service" in change.what and "ticket-service" in change.what


def test_removing_a_nested_field_a_consumer_reads_is_major(new):
    items = new.schemas["EventPublished"]["properties"]["ticket_categories"]["items"]
    del items["properties"]["name"]
    items["required"].remove("name")
    change = only(new)
    assert change.level == "major"
    assert "`ticket_categories[].name`" in change.what


def test_removing_an_object_reports_only_the_object(new):
    schema = new.schemas["EventPublished"]
    del schema["properties"]["ticket_categories"]
    schema["required"].remove("ticket_categories")
    assert "`ticket_categories`" in only(new).what


def test_making_a_field_a_consumer_reads_optional_is_major(new):
    new.schemas["EventPublished"]["required"].remove("venue")
    change = only(new)
    assert change.level == "major"
    assert "ticket-service" in change.what


def test_new_required_field_is_major(new):
    schema = new.schemas["OrderConfirmed"]
    schema["properties"]["promo_code"] = {"type": "string"}
    schema["required"].append("promo_code")
    assert only(new).level == "major"


def test_optional_field_made_required_is_major(new):
    schema = new.schemas["OrderConfirmed"]
    schema["properties"]["promo_code"] = {"type": "string"}
    base = copy.deepcopy(new)
    schema["required"].append("promo_code")
    assert [c.level for c in compat.diff(base, new)] == ["major"]


@pytest.mark.parametrize(
    "edit",
    [
        lambda p: p["quantity"].update(type="string"),
        lambda p: p["quantity"].update(minimum=0),
        lambda p: p["currency"].update(const="USD"),
        lambda p: p["delivery_email"].update(pattern="^.+$"),
    ],
    ids=["type", "minimum", "const", "pattern"],
)
def test_any_type_or_constraint_change_is_major(new, edit):
    edit(new.schemas["OrderConfirmed"]["properties"])
    assert only(new).level == "major"


def test_changing_a_shared_definition_is_major_for_every_field_using_it(new):
    new.schemas["EventSoldOut"]["$defs"]["uuid"]["pattern"] = "^.+$"
    assert {c.level for c in changes(new)} == {"major"}


def test_changed_routing_key_is_major(new):
    new.messages["EventSoldOut"]["routing_key"] = "event.sold.out"
    change = only(new)
    assert change.level == "major"
    assert change.services == {"reservation-service", "queue-service"}


def test_changed_publisher_is_major(new):
    new.messages["EventSoldOut"]["publisher"] = "queue-service"
    assert only(new).level == "major"


def test_removed_consumer_is_major(new):
    new.messages["EventSoldOut"]["consumers"].remove("queue-service")
    assert only(new).level == "major"


def test_removed_message_is_major(new):
    del new.messages["TicketIssued"]
    assert only(new).level == "major"


def test_removed_schema_is_major(new):
    del new.schemas["EventSoldOut"]
    assert only(new).level == "major"


def test_envelope_change_is_major_for_every_service(new):
    new.envelope["properties"]["trace_id"] = {"type": "string"}
    change = only(new)
    assert change.level == "major"
    assert change.services == CURRENT.services()


def test_envelope_description_change_is_patch(new):
    new.envelope["properties"]["producer"]["description"] = "Who sent it."
    assert only(new).level == "patch"


# ---------- summary and versions ----------


def test_no_changes():
    assert changes(copy.deepcopy(CURRENT)) == []
    assert compat.required_level([]) == "none"


def test_required_level_and_affected_services_combine_changes(new):
    new.messages["EventSoldOut"]["routing_key"] = "event.sold.out"
    new.requirements["user-service"]["OrderConfirmed"].remove("event_id")
    found = changes(new)
    assert compat.required_level(found) == "major"
    assert found[0].level == "major"  # most severe first
    assert compat.affected_services(found) == {
        "reservation-service",
        "queue-service",
        "user-service",
    }


@pytest.mark.parametrize(
    ("base", "new_version", "level", "ok"),
    [
        ("1.0.0", "1.0.0", "none", True),
        ("1.0.0", "1.0.1", "patch", True),
        ("1.0.0", "1.0.0", "patch", False),
        ("1.0.0", "1.1.0", "patch", True),
        ("1.0.0", "1.0.1", "minor", False),
        ("1.0.0", "1.1.0", "minor", True),
        ("1.0.0", "2.0.0", "minor", True),
        ("1.4.2", "1.5.0", "major", False),
        ("1.4.2", "2.0.0", "major", True),
        ("1.4.2", "1.4.1", "none", False),  # never down
    ],
)
def test_bump_problem(base, new_version, level, ok):
    assert (compat.bump_problem(base, new_version, level) is None) == ok


def test_main_fails_when_the_bump_is_too_small(tmp_path, monkeypatch, capsys):
    base = copy.deepcopy(CURRENT)
    base.messages["EventSoldOut"]["routing_key"] = "event.sold.out"
    bundles.write_bundle(base, tmp_path)
    monkeypatch.setattr(bundles, "collect", lambda *a, **k: CURRENT)
    assert compat.main(["--base-dir", str(tmp_path)]) == 1
    needed = compat.minimum_version(CURRENT.version, "major")
    assert f"major change needs VERSION {needed}" in capsys.readouterr().err


def test_main_prints_affected_services(tmp_path, monkeypatch, capsys):
    base = copy.deepcopy(CURRENT)
    base.messages["EventSoldOut"]["routing_key"] = "event.sold.out"
    bundles.write_bundle(base, tmp_path)
    monkeypatch.setattr(bundles, "collect", lambda *a, **k: CURRENT)
    assert compat.main(["--base-dir", str(tmp_path), "--affected"]) == 0
    assert capsys.readouterr().out.split() == ["queue-service", "reservation-service"]


def test_bundle_catalogue_is_plain_json():
    # The Java package reads catalogue.json with Jackson: no Python-only values.
    json.loads(json.dumps(CURRENT.catalogue))
