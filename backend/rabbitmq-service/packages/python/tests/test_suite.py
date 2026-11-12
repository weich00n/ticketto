"""ticketto_contracts.suite, run the way a service runs it: a sample reservation-service (it
publishes and consumes) with a test module and conftest, inside pytester."""

import pytest

SAMPLE_CONFTEST = """
import json

import pytest

from ticketto_contracts import catalogue


class Props:  # stands in for pika.BasicProperties
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


SERVICE = "reservation-service"
ROUTING_KEYS = {{m: catalogue.message(m).routing_key for m in catalogue.published_by(SERVICE)}}
ROUTING_KEYS.update({routing_overrides})
LISTENERS = {listeners}


def relay(name, channel):
    body = catalogue.example(name)
    channel.exchange_declare("reservation.events", exchange_type="topic", durable=True)
    {extra_relay_line}
    channel.confirm_delivery()
    channel.basic_publish(
        exchange="reservation.events",
        routing_key=ROUTING_KEYS[name],
        body=json.dumps(body),
        properties=Props(
            content_type="application/json",
            delivery_mode=2,
            message_id=body["message_id"],
            type=name,
            correlation_id="c-1",
        ),
        mandatory=True,
    )


@pytest.fixture
def contract_publish():
    return relay


@pytest.fixture
def contract_queues():
    return LISTENERS


@pytest.fixture
def contract_handle():
    def handle(queue, body, properties):
        payload = json.loads(body)["payload"]
        {handler_body}
    return handle
"""

GOOD_HANDLER = 'return "ack" if "event_id" in payload and "event_version" in payload else "reject"'
GOOD_LISTENERS = ["reservation-service.EventPublished", "reservation-service.EventCancelled"]


def make_service(  # pylint: disable=too-many-arguments  # keyword-only knobs
    pytester,
    *,
    module_extra="",
    service="reservation-service",
    routing_overrides=None,
    listeners=None,
    extra_relay_line="pass",
    handler_body=GOOD_HANDLER,
    with_fixtures=True,
):
    conftest = SAMPLE_CONFTEST.format(
        routing_overrides=routing_overrides or {},
        listeners=GOOD_LISTENERS if listeners is None else listeners,
        extra_relay_line=extra_relay_line,
        handler_body=handler_body,
    )
    if not with_fixtures:
        conftest = conftest.split("@pytest.fixture")[0]
    pytester.makeconftest(conftest)
    pytester.makepyfile(
        test_contracts=(
            "from ticketto_contracts.suite import *  # noqa: F403\n"
            f"CONTRACT_SERVICE = {service!r}\n{module_extra}\n"
        )
    )


def test_a_service_that_keeps_the_contract_passes(pytester):
    make_service(pytester)
    result = pytester.runpytest("-v")
    # 3 publishes, the listener check, 2 minimal bodies, the known-gaps check
    result.assert_outcomes(passed=6)
    result.stdout.fnmatch_lines(
        ["*test_publishes?EventSoldOut? PASSED*", "*test_accepts?EventPublished-minimal? PASSED*"]
    )


def test_a_wrong_routing_key_fails_with_the_problem(pytester):
    make_service(pytester, routing_overrides={"EventSoldOut": "event.sold.out"})
    result = pytester.runpytest()
    result.assert_outcomes(passed=5, failed=1)
    result.stdout.fnmatch_lines(
        ["*routing_key: routing key 'event.sold.out', not 'event.sold_out'*"]
    )


def test_declaring_a_queue_is_reported(pytester):
    make_service(
        pytester,
        extra_relay_line=(
            'channel.queue_declare("reservation-service.EventPublished", durable=True)'
        ),
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=3, failed=3)
    result.stdout.fnmatch_lines(["*queue_declare*the broker owns its queues*"])


def test_a_missing_listener_fails(pytester):
    make_service(
        pytester, listeners=["reservation-service.EventPublished", "reservation-service.dlx"]
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=5, failed=1)
    result.stdout.fnmatch_lines(
        [
            "*extra_queue: reservation-service.dlx*",
            "*missing_queue: reservation-service.EventCancelled is not listened on*",
        ]
    )


def test_a_handler_that_needs_an_optional_or_missing_field_fails(pytester):
    make_service(pytester, handler_body='return "ack" if "seat_map" in payload else "requeue"')
    result = pytester.runpytest()
    result.assert_outcomes(passed=4, failed=2)
    result.stdout.fnmatch_lines(["*the handler returned 'requeue'; expected 'ack'*"])


def test_opt_in_bodies(pytester):
    make_service(pytester, module_extra='CONTRACT_BODIES = ["extra-fields", "invalid"]')
    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=10)
    result.stdout.fnmatch_lines(["*test_accepts?EventCancelled-invalid? PASSED*"])


def test_requeueing_an_invalid_body_fails(pytester):
    make_service(
        pytester,
        module_extra='CONTRACT_BODIES = ["invalid"]',
        handler_body='return "ack" if "event_version" in payload else "requeue"',
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=6, failed=2)
    result.stdout.fnmatch_lines(["*expected 'reject'*"])


def test_known_gaps_are_refused(pytester):
    make_service(
        pytester,
        module_extra='CONTRACT_KNOWN_GAPS = {"test_publishes[EventSoldOut]": "https://issues/42"}',
    )
    result = pytester.runpytest()
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*CONTRACT_KNOWN_GAPS is no longer supported*"])


def test_a_publisher_without_the_fixture_fails(pytester):
    make_service(pytester, with_fixtures=False)
    result = pytester.runpytest()
    result.assert_outcomes(passed=0, failed=6)
    result.stdout.fnmatch_lines(["*define the `contract_publish` fixture*"])


def test_a_consumer_only_service_skips_the_publisher_tests(pytester):
    make_service(
        pytester,
        service="user-service",
        listeners=["user-service.EventCancelled", "user-service.OrderConfirmed"],
        handler_body='return "ack"',
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=3, skipped=1)


@pytest.mark.parametrize("value", ["None", "'billing-service'"])
def test_contract_service_must_be_a_catalogued_service(pytester, value):
    pytester.makepyfile(
        test_contracts=(
            f"from ticketto_contracts.suite import *  # noqa: F403\nCONTRACT_SERVICE = {value}\n"
        )
    )
    result = pytester.runpytest()
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*set CONTRACT_SERVICE to one of*"])
