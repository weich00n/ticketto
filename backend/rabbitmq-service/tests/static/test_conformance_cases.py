"""packages/conformance/cases.json stays true to the contracts.

Both packages must agree with these cases, so a case that has gone stale (a schema changed, a
consumer was added) would make both wrong together. The envelope, payload and listener verdicts
are rechecked here against the real schemas and catalogue; the property and routing checks are
the packages' own and are covered by their suites.
"""

import copy
import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tools import build_contracts_bundle as bundles

CASES = json.loads(
    (bundles.ROOT / "packages" / "conformance" / "cases.json").read_text(encoding="utf-8")
)
PUBLISH = CASES["publish"]
LISTENERS = CASES["listeners"]
BUNDLE = bundles.collect()
ROOTS = ("exchange", "routing_key", "mandatory", "confirmed", "properties.", "body.")


def _step(container: Any, key: str) -> tuple[Any, Any]:
    return (container, int(key)) if isinstance(container, list) else (container, key)


def _walk(target: dict, path: str) -> tuple[Any, Any]:
    *parents, last = path.split(".")
    node: Any = target
    for key in parents:
        container, k = _step(node, key)
        node = container[k]
    return _step(node, last)


def baseline(message: str) -> dict[str, Any]:
    entry, example = BUNDLE.messages[message], BUNDLE.examples[message]
    return {
        "service": entry["publisher"],
        "exchange": entry["exchange"],
        "routing_key": entry["routing_key"],
        "mandatory": True,
        "confirmed": True,
        "properties": {
            "content_type": "application/json",
            "delivery_mode": 2,
            "message_id": example["message_id"],
            "type": message,
            "correlation_id": "corr-conformance-1",
        },
        "body": copy.deepcopy(example),
    }


def apply(case: dict[str, Any]) -> dict[str, Any]:
    published = baseline(case["message"])
    if "service" in case:
        published["service"] = case["service"]
    for path, value in case.get("set", {}).items():
        container, key = _walk(published, path)
        container[key] = value
    for path in case.get("remove", []):
        container, key = _walk(published, path)
        del container[key]
    if "raw_body" in case:
        published["body"] = case["raw_body"]  # bytes on the wire; not a JSON object here
    return published


# ---------- publish cases ----------


def test_publish_case_ids_are_unique():
    ids = [c["id"] for c in PUBLISH["cases"]]
    assert len(ids) == len(set(ids))


def test_every_message_with_an_example_has_a_valid_case():
    valid = {c["message"] for c in PUBLISH["cases"] if c["id"].startswith("valid-")}
    assert valid == set(BUNDLE.examples)


@pytest.mark.parametrize("case", PUBLISH["cases"], ids=lambda c: c["id"])
def test_publish_case_is_well_formed(case):
    assert case["message"] in BUNDLE.examples, "the baseline needs the message's example"
    assert set(case["expect"]) <= set(PUBLISH["codes"]), "unknown problem code"
    if "service" in case:
        assert case["service"] in BUNDLE.services()
    for path in [*case.get("set", {}), *case.get("remove", [])]:
        assert path.startswith(ROOTS), path
    apply(case)  # every path resolves


@pytest.mark.parametrize("case", PUBLISH["cases"], ids=lambda c: c["id"])
def test_publish_case_envelope_and_payload_verdicts_match_the_schemas(case):
    body = apply(case)["body"]
    if not isinstance(body, dict):
        assert "body_json" in case["expect"]
        return
    envelope_ok = Draft202012Validator(BUNDLE.envelope).is_valid(body)
    assert envelope_ok == ("envelope" not in case["expect"])

    named = apply(case)["properties"].get("type") or body.get("message_type")
    schema = BUNDLE.schemas.get(named)
    if schema is None or not isinstance(body.get("payload"), dict):
        assert "payload" not in case["expect"]
        return
    payload_ok = Draft202012Validator(schema).is_valid(body["payload"])
    assert payload_ok == ("payload" not in case["expect"])


# ---------- listener cases ----------


@pytest.mark.parametrize("case", LISTENERS["cases"], ids=lambda c: c["id"])
def test_listener_case_matches_the_catalogue(case):
    expected = set(BUNDLE.catalogue["services"][case["service"]]["queues"])
    listened = set(case["queues"])
    verdict = {f"missing_queue:{q}" for q in expected - listened} | {
        f"extra_queue:{q}" for q in listened - expected
    }
    assert verdict == set(case["expect"])
