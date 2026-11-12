"""Contract tests for one service, generated from the bundled catalogue.

In the service, one test module:

    # tests/contract/test_contracts.py
    from ticketto_contracts.suite import *  # noqa: F403

    CONTRACT_SERVICE = "checkout-service"
    CONTRACT_BODIES = []        # optional: extra consumer bodies, e.g. ["extra-fields", "invalid"]

and fixtures in its conftest.py, for the roles the catalogue gives the service:

    contract_publish   publishers: a function (message_name, channel) that makes the service's
                       real outbox relay publish that message on `channel`, a RecordingChannel
    contract_queues    consumers: the queue names the service listens on
    contract_handle    consumers: a function (queue, body: bytes, properties: dict) that runs the
                       real handler and returns "ack", "reject" or "requeue"

Generated tests:

    test_publishes[<Message>]           one per message the service publishes
    test_listens_on_catalogued_queues   its listeners are exactly its catalogued queues
    test_accepts[<Message>-minimal]     one per message it consumes: the handler acks a body
                                        with only the publisher's required fields
    test_accepts[<Message>-<case>]      the CONTRACT_BODIES cases ("invalid" must be rejected)

There is no way to mark a test as an expected failure: a service that doesn't match the
contracts fails until it, or the contract, is fixed.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest

from ticketto_contracts import bodies, catalogue
from ticketto_contracts.listeners import assert_listener_queues
from ticketto_contracts.recording import RecordingChannel
from ticketto_contracts.validate import ContractError, Problem, check_publish

__all__ = [
    "pytest_generate_tests",
    "test_publishes",
    "test_listens_on_catalogued_queues",
    "test_accepts",
]

OUTCOMES = ("ack", "reject", "requeue")


def _service(module: ModuleType) -> str:
    if hasattr(module, "CONTRACT_KNOWN_GAPS"):
        raise pytest.UsageError(
            f"{module.__name__}: CONTRACT_KNOWN_GAPS is no longer supported (ticketto-contracts "
            "2.0.0): remove it. A mismatch with the contracts must fail."
        )
    service = getattr(module, "CONTRACT_SERVICE", None)
    if service not in catalogue.services():
        raise pytest.UsageError(
            f"{module.__name__}: set CONTRACT_SERVICE to one of {', '.join(catalogue.services())} "
            f"(got {service!r})"
        )
    return service


def _body_cases(module: ModuleType) -> list[str]:
    extra = list(getattr(module, "CONTRACT_BODIES", []))
    unknown = set(extra) - set(bodies.CASES)
    if unknown:
        raise pytest.UsageError(
            f"CONTRACT_BODIES: unknown cases {sorted(unknown)}; use {bodies.CASES}"
        )
    return ["minimal", *[c for c in extra if c != "minimal"]]


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    service = _service(metafunc.module)
    name = metafunc.function.__name__
    if name == "test_publishes":
        published = catalogue.published_by(service)
        metafunc.parametrize(
            "contract_message",
            published
            or [pytest.param(None, marks=pytest.mark.skip(reason=f"{service} publishes nothing"))],
            ids=published or ["none"],
        )
    elif name == "test_accepts":
        consumed = catalogue.consumed_by(service)
        cases = [(m, c) for m in consumed for c in _body_cases(metafunc.module)]
        metafunc.parametrize(
            ("contract_message", "contract_body"),
            cases
            or [
                pytest.param(
                    None, None, marks=pytest.mark.skip(reason=f"{service} consumes nothing")
                )
            ],
            ids=[f"{m}-{c}" for m, c in cases] or ["none"],
        )


def _fixture(request: pytest.FixtureRequest, name: str, role: str) -> Any:
    try:
        return request.getfixturevalue(name)
    except pytest.FixtureLookupError:
        raise pytest.fail.Exception(
            f"{_service(request.module)} {role} in the catalogue: define the `{name}` fixture "
            "in conftest.py (see ticketto_contracts.suite)",
            pytrace=False,
        ) from None


def test_publishes(request: pytest.FixtureRequest, contract_message: str) -> None:
    service = _service(request.module)
    publish = _fixture(request, "contract_publish", "publishes messages")

    channel = RecordingChannel(service=service)
    publish(contract_message, channel)
    problems: list[Problem] = list(channel.violations)
    sent = [p for p in channel.published if p.properties.get("type") == contract_message]
    if not channel.published:
        problems.append(
            Problem(
                "nothing_published", f"contract_publish({contract_message!r}) published nothing"
            )
        )
    elif not sent:
        types = sorted({str(p.properties.get("type")) for p in channel.published})
        problems.append(
            Problem(
                "wrong_message",
                f"asked for {contract_message}, published type {', '.join(types)}",
            )
        )
    for published in channel.published:
        problems += check_publish(service, published)
    if problems:
        raise ContractError(f"{service} publishing {contract_message}", problems)


def test_listens_on_catalogued_queues(request: pytest.FixtureRequest) -> None:
    service = _service(request.module)
    if not catalogue.consumed_by(service):
        pytest.skip(f"{service} consumes nothing")
    queues = _fixture(request, "contract_queues", "consumes messages")
    assert_listener_queues(service, queues)


def test_accepts(request: pytest.FixtureRequest, contract_message: str, contract_body: str) -> None:
    service = _service(request.module)
    handle = _fixture(request, "contract_handle", "consumes messages")
    queue = f"{service}.{contract_message}"
    expected = "reject" if contract_body == "invalid" else "ack"

    body, properties = bodies.delivery(contract_message, contract_body)
    outcome = handle(queue, body, properties)
    if outcome not in OUTCOMES:
        raise ContractError(
            f"{service} handling {contract_message}",
            [Problem("outcome", f"contract_handle returned {outcome!r}, not one of {OUTCOMES}")],
        )
    if outcome != expected:
        raise ContractError(
            f"{service} handling {contract_message} ({contract_body} body)",
            [Problem("outcome", f"the handler returned {outcome!r}; expected {expected!r}")],
        )
