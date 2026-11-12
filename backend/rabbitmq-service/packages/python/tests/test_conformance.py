"""packages/conformance/cases.json: the verdicts this package must share with the Java one."""

import copy
import json
from typing import Any

import pytest
from conftest import CASES
from ticketto_contracts import PublishedMessage, catalogue, check_listener_queues, check_publish

SPEC = json.loads(CASES.read_text(encoding="utf-8"))


def _step(container: Any, key: str) -> tuple[Any, Any]:
    return (container, int(key)) if isinstance(container, list) else (container, key)


def _walk(target: dict, path: str) -> tuple[Any, Any]:
    *parents, last = path.split(".")
    node: Any = target
    for key in parents:
        container, k = _step(node, key)
        node = container[k]
    return _step(node, last)


def _published(case: dict[str, Any]) -> tuple[str, PublishedMessage]:
    """The baseline publish for the case's message, with the case's changes applied."""
    message = catalogue.message(case["message"])
    example = catalogue.example(case["message"])
    draft: dict[str, Any] = {
        "service": message.publisher,
        "exchange": message.exchange,
        "routing_key": message.routing_key,
        "mandatory": True,
        "confirmed": True,
        "properties": {
            "content_type": "application/json",
            "delivery_mode": 2,
            "message_id": example["message_id"],
            "type": message.name,
            "correlation_id": "corr-conformance-1",
        },
        "body": copy.deepcopy(example),
    }
    if "service" in case:
        draft["service"] = case["service"]
    for path, value in case.get("set", {}).items():
        container, key = _walk(draft, path)
        container[key] = value
    for path in case.get("remove", []):
        container, key = _walk(draft, path)
        del container[key]
    body = case["raw_body"].encode() if "raw_body" in case else json.dumps(draft["body"]).encode()
    return draft["service"], PublishedMessage(
        exchange=draft["exchange"],
        routing_key=draft["routing_key"],
        body=body,
        properties=draft["properties"],
        mandatory=draft["mandatory"],
        confirmed=draft["confirmed"],
    )


@pytest.mark.parametrize("case", SPEC["publish"]["cases"], ids=lambda c: c["id"])
def test_publish_case(case):
    service, published = _published(case)
    problems = check_publish(service, published)
    assert {p.code for p in problems} == set(case["expect"]), [str(p) for p in problems]


@pytest.mark.parametrize("case", SPEC["listeners"]["cases"], ids=lambda c: c["id"])
def test_listener_case(case):
    assert set(check_listener_queues(case["service"], case["queues"])) == set(case["expect"])


def test_every_problem_code_is_known():
    codes = set(SPEC["publish"]["codes"])
    for case in SPEC["publish"]["cases"]:
        _, published = _published(case)
        assert {p.code for p in check_publish("checkout-service", published)} <= codes
