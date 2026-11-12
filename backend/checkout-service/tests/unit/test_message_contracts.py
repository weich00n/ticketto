"""Checkout's messages against the broker contract (rabbitmq-service) and against what
reservation-service writes (OutboxWriter), using the fixtures in tests/contracts/.

The broker-contract checks read the sibling rabbitmq-service repo when it is checked out
next to this one (the assembled g2-team5 layout) and skip otherwise, so a single-repo CI
still runs everything else.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.messaging import events
from app.messaging.consumers import _dispose
from app.messaging.envelope import PoisonMessage, parse
from app.messaging.outbox import EXCHANGE, build_envelope, message_id_for
from app.messaging.payloads import (
    QUEUE_MESSAGE_TYPES,
    event_cancelled_fields,
    refund_required_fields,
    reservation_expired_fields,
)

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
BROKER_REPO = Path(__file__).resolve().parents[3] / "rabbitmq-service"


def _fixture(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _props(message_type: str | None = None, correlation_id: str | None = "c-1"):
    return SimpleNamespace(correlation_id=correlation_id, type=message_type, headers=None)


def _body(envelope: dict) -> bytes:
    return json.dumps(envelope).encode()


# --------------------------------------------------------------------- inbound


def test_refund_required_as_reservation_must_publish_it_is_accepted():
    fx = _fixture("reservation.RefundRequired.json")
    seen = []
    ack, requeue = _dispose(
        _body(fx["body"]),
        _props(fx["amqp"]["type"]),
        seen.append,
        QUEUE_MESSAGE_TYPES["checkout-service.RefundRequired"],
    )
    assert (ack, requeue) == (True, False)
    fields = refund_required_fields(seen[0].payload)
    assert fields.order_id == fx["body"]["payload"]["order_id"]
    assert fields.hold_id == fx["body"]["payload"]["hold_id"]
    assert fields.total_amount == 57600
    assert seen[0].correlation_id == "c-1"  # from the AMQP property, not the body


@pytest.mark.parametrize(
    "case", sorted(_fixture("reservation.RefundRequired.invalid.json")["cases"])
)
def test_known_wrong_refund_required_shapes_are_dead_lettered(case):
    body = _fixture("reservation.RefundRequired.invalid.json")["cases"][case]

    def handle(envelope):
        refund_required_fields(envelope.payload)

    ack, requeue = _dispose(_body(body), _props(), handle, "RefundRequired")
    assert (ack, requeue) == (False, False), case


def test_reservation_expired_minimum_payload_is_accepted():
    fx = _fixture("reservation.ReservationExpired.json")
    envelope = parse(_body(fx["body"]))
    assert reservation_expired_fields(envelope.payload) == fx["body"]["payload"]["hold_id"]


def test_a_message_on_the_wrong_queue_is_dead_lettered():
    fx = _fixture("reservation.RefundRequired.json")
    ack, requeue = _dispose(
        _body(fx["body"]), _props("RefundRequired"), lambda _e: None, "EventCancelled"
    )
    assert (ack, requeue) == (False, False)


def test_amqp_type_must_agree_with_the_envelope():
    fx = _fixture("reservation.RefundRequired.json")
    ack, requeue = _dispose(
        _body(fx["body"]), _props("ReservationExpired"), lambda _e: None, "RefundRequired"
    )
    assert (ack, requeue) == (False, False)


def test_a_transient_handler_failure_is_requeued():
    fx = _fixture("reservation.RefundRequired.json")

    def boom(_envelope):
        raise RuntimeError("database down")

    assert _dispose(_body(fx["body"]), _props(), boom, "RefundRequired") == (False, True)


@pytest.mark.parametrize(
    ("validator", "payload"),
    [
        (event_cancelled_fields, {}),
        (event_cancelled_fields, {"event_id": ""}),
        (event_cancelled_fields, {"event_id": 7}),
        (reservation_expired_fields, {"hold_id": None}),
        (refund_required_fields, {"order_id": "o", "hold_id": "h", "currency": "SGD"}),
        (
            refund_required_fields,
            {"order_id": "o", "hold_id": "h", "total_amount": True, "currency": "SGD"},
        ),
    ],
)
def test_unusable_payloads_are_poison(validator, payload):
    with pytest.raises(PoisonMessage):
        validator(payload)


def test_unknown_payload_fields_are_ignored():
    # The broker contracts: consumers must ignore fields they don't know.
    assert event_cancelled_fields({"event_id": "e1", "new_field": {"x": 1}}) == "e1"


# -------------------------------------------------------------------- outbound


def _sample_payloads() -> dict[str, dict]:
    order = SimpleNamespace(
        order_id="9b8a7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d",
        hold_id="3f1c2b9e-8a7d-4c6e-9b1a-2d3e4f5a6b7c",
        admission_id="a-1",
        user_id="u-1",
        event_id="e-1",
        category_id="c-1",
        quantity=2,
        delivery_email="a@b.co",
        total_amount=57600,
        currency="SGD",
        confirmed_at=None,
    )
    refund = SimpleNamespace(
        refund_id="r-1", amount=57600, currency="SGD", reason="event_cancelled", last_error="x"
    )
    return {
        events.EVENT_ORDER_CONFIRMED: events.order_confirmed_data(order),
        events.EVENT_REFUND_COMPLETED: events.refund_completed_data(order, refund),
        events.EVENT_ORDER_REFUNDED: events.order_refunded_data(order, refund),
        events.EVENT_REFUND_FAILED: events.refund_failed_data(order, refund),
    }


def test_every_outbound_envelope_round_trips_through_a_strict_parser():
    for message_type, payload in _sample_payloads().items():
        env = build_envelope(message_type, payload, message_id_for(message_type, "o-1"))
        assert parse(_body(env)).message_type == message_type


@pytest.mark.skipif(not BROKER_REPO.exists(), reason="rabbitmq-service repo not checked out")
def test_outbound_matches_the_broker_catalogue_and_envelope_schema():
    jsonschema = pytest.importorskip("jsonschema")
    yaml = pytest.importorskip("yaml")
    schema = json.loads((BROKER_REPO / "contracts/envelope.schema.json").read_text("utf-8"))
    catalogue = yaml.safe_load((BROKER_REPO / "topology/messages.yaml").read_text("utf-8"))
    messages = catalogue["messages"]

    assert EXCHANGE == "checkout.events"
    ours = {name for name, m in messages.items() if m["publisher"] == "checkout-service"}
    assert ours == set(events.ROUTING_KEYS)
    for message_type, payload in _sample_payloads().items():
        assert events.ROUTING_KEYS[message_type] == messages[message_type]["routing_key"]
        env = build_envelope(message_type, payload, message_id_for(message_type, "o-1"))
        jsonschema.validate(env, schema)

    consumed = {
        f"checkout-service.{name}"
        for name, m in messages.items()
        if "checkout-service" in m.get("consumers", [])
    }
    assert consumed == set(QUEUE_MESSAGE_TYPES)


@pytest.mark.skipif(not BROKER_REPO.exists(), reason="rabbitmq-service repo not checked out")
def test_inbound_fixtures_match_the_broker_envelope_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((BROKER_REPO / "contracts/envelope.schema.json").read_text("utf-8"))
    for name in ("reservation.RefundRequired.json", "reservation.ReservationExpired.json"):
        jsonschema.validate(_fixture(name)["body"], schema)
    example = json.loads(
        (BROKER_REPO / "contracts/examples/EventCancelled.json").read_text("utf-8")
    )
    assert event_cancelled_fields(parse(_body(example)).payload)
