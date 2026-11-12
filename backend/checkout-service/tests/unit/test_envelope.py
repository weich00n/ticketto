import json

import pytest

from app.messaging.envelope import PoisonMessage, parse

# The shape event-service publishes (rabbitmq-service contracts/examples/EventCancelled.json).
GOOD = {
    "message_id": "018f3c0e-1111-4222-8333-444455556666",
    "message_type": "EventCancelled",
    "schema_version": 1,
    "occurred_at": "2026-11-20T12:00:00.500Z",
    "producer": "event-service",
    "payload": {"event_id": "e-1"},
}


def _b(obj) -> bytes:
    return json.dumps(obj).encode()


def test_good_envelope_parses():
    env = parse(_b(GOOD), "corr-1")
    assert env.message_type == "EventCancelled"
    assert env.message_id == GOOD["message_id"]
    assert env.producer == "event-service"
    assert env.payload == {"event_id": "e-1"}
    # The correlation id comes from the AMQP property, not the body.
    assert env.correlation_id == "corr-1"


def test_missing_correlation_property_is_allowed():
    assert parse(_b(GOOD)).correlation_id is None


def test_legacy_event_type_data_shape_is_poison():
    # The retired ticketto.events shape must not be half-accepted.
    legacy = {
        "message_id": GOOD["message_id"],
        "event_type": "EventCancelled",
        "schema_version": 1,
        "occurred_at": GOOD["occurred_at"],
        "correlation_id": "abc123",
        "data": {"event_id": "e-1"},
    }
    with pytest.raises(PoisonMessage):
        parse(_b(legacy))


def test_not_json_is_poison():

    with pytest.raises(PoisonMessage):
        parse(b"not json at all")


def test_non_object_is_poison():

    with pytest.raises(PoisonMessage):
        parse(_b([1, 2, 3]))


def test_missing_field_is_poison():

    bad = dict(GOOD)
    del bad["producer"]
    with pytest.raises(PoisonMessage):
        parse(_b(bad))


def test_extra_field_is_poison():

    bad = dict(GOOD)
    bad["extra"] = "nope"
    with pytest.raises(PoisonMessage):
        parse(_b(bad))


def test_bad_message_id_is_poison():

    bad = dict(GOOD)
    bad["message_id"] = "not-a-uuid"
    with pytest.raises(PoisonMessage):
        parse(_b(bad))


def test_bad_message_type_is_poison():

    bad = dict(GOOD)
    bad["message_type"] = "event.cancelled"  # the routing key, not the PascalCase name
    with pytest.raises(PoisonMessage):
        parse(_b(bad))


def test_bad_schema_version_is_poison():

    bad = dict(GOOD)
    bad["schema_version"] = 0
    with pytest.raises(PoisonMessage):
        parse(_b(bad))


def test_unknown_schema_version_is_poison():

    bad = dict(GOOD)
    bad["schema_version"] = 2
    with pytest.raises(PoisonMessage):
        parse(_b(bad))


def test_string_schema_version_is_poison():

    bad = dict(GOOD)
    bad["schema_version"] = "1"
    with pytest.raises(PoisonMessage):
        parse(_b(bad))


def test_bad_producer_is_poison():

    bad = dict(GOOD)
    bad["producer"] = "EventService"
    with pytest.raises(PoisonMessage):
        parse(_b(bad))


def test_bad_occurred_at_is_poison():

    bad = dict(GOOD)
    bad["occurred_at"] = "2026-11-20 12:00:00"  # no T / no Z
    with pytest.raises(PoisonMessage):
        parse(_b(bad))


def test_payload_not_object_is_poison():

    bad = dict(GOOD)
    bad["payload"] = "a string"
    with pytest.raises(PoisonMessage):
        parse(_b(bad))
