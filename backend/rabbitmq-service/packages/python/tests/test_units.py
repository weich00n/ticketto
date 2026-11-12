"""The package's modules, one behaviour at a time."""

import enum
import json

import pytest
from jsonschema import Draft202012Validator
from ticketto_contracts import (
    ContractError,
    RecordingChannel,
    assert_listener_queues,
    assert_valid_publish,
    bodies,
    catalogue,
)
from ticketto_contracts.validate import properties_dict

# ---------- catalogue ----------


def test_catalogue_roles():
    assert catalogue.published_by("reservation-service") == [
        "ReservationExpired",
        "RefundRequired",
        "EventSoldOut",
    ]
    assert catalogue.consumed_by("user-service") == ["EventCancelled", "OrderConfirmed"]
    assert catalogue.exchange_for("checkout-service") == "checkout.events"
    assert catalogue.exchange_for("user-service") is None
    assert catalogue.message("EventSoldOut").routing_key == "event.sold_out"
    assert catalogue.message("Nope") is None


def test_unknown_service_is_an_error():
    with pytest.raises(ValueError, match="not in the catalogue"):
        catalogue.published_by("billing-service")


def test_catalogue_returns_copies():
    catalogue.example("OrderConfirmed")["payload"]["quantity"] = 99
    assert catalogue.example("OrderConfirmed")["payload"]["quantity"] != 99


def test_requirements():
    assert catalogue.requirements("user-service")["OrderConfirmed"] == ["user_id", "event_id"]
    assert catalogue.requirements("event-service") == {}


# ---------- bodies ----------

SCHEMA = {
    "type": "object",
    "required": ["id", "items"],
    "properties": {
        "id": {"$ref": "#/$defs/id"},
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
    "$defs": {"id": {"type": "string"}},
}


def test_strip_optional_keeps_only_required_fields_at_every_level():
    value = {"id": "a", "note": "x", "items": [{"name": "n", "size": 2}], "unknown": 1}
    assert bodies.strip_optional(value, SCHEMA, SCHEMA) == {"id": "a", "items": [{"name": "n"}]}


@pytest.mark.parametrize("name", catalogue.consumed_by("ticket-service"))
def test_generated_bodies_against_the_schema(name):
    schema = catalogue.payload_schema(name)
    validator = Draft202012Validator(schema)
    assert validator.is_valid(bodies.minimal(name)["payload"])
    assert validator.is_valid(bodies.extra_fields(name)["payload"])  # unknown fields are allowed
    assert not validator.is_valid(bodies.invalid(name)["payload"])


def test_extra_fields_reach_nested_objects():
    payload = bodies.extra_fields("EventPublished")["payload"]
    assert bodies.UNKNOWN_FIELD in payload
    assert bodies.UNKNOWN_FIELD in payload["ticket_categories"][0]


def test_delivery_is_what_a_good_publisher_sends():
    body, properties = bodies.delivery("OrderConfirmed")
    assert json.loads(body)["message_type"] == "OrderConfirmed"
    assert properties["type"] == "OrderConfirmed"
    assert properties["delivery_mode"] == 2


def test_unknown_body_case():
    with pytest.raises(ValueError, match="unknown body case"):
        bodies.body("OrderConfirmed", "huge")


# ---------- recording channel and validate ----------


class DeliveryMode(enum.Enum):  # like pika.DeliveryMode
    PERSISTENT = 2


class Props:  # like pika.BasicProperties
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _publish_order_confirmed(channel, **overrides):
    body = bodies.example("OrderConfirmed")
    props = {
        "content_type": "application/json",
        "delivery_mode": DeliveryMode.PERSISTENT,
        "message_id": body["message_id"],
        "type": "OrderConfirmed",
        "correlation_id": "c-1",
    }
    channel.basic_publish(
        exchange=overrides.pop("exchange", "checkout.events"),
        routing_key=overrides.pop("routing_key", "order.confirmed"),
        body=json.dumps(body),
        properties=Props(**(props | overrides)),
        mandatory=True,
    )


def test_recording_channel_records_a_pika_style_publish():
    channel = RecordingChannel(service="checkout-service")
    channel.confirm_delivery()
    _publish_order_confirmed(channel)
    (published,) = channel.published
    assert published.properties["delivery_mode"] == 2  # enum value
    assert published.confirmed and published.mandatory
    assert_valid_publish("checkout-service", published)


def test_publish_before_confirm_delivery_is_not_confirmed():
    channel = RecordingChannel()
    _publish_order_confirmed(channel)
    with pytest.raises(ContractError, match="confirms"):
        assert_valid_publish("checkout-service", channel.published[0])


def test_contract_error_lists_every_problem():
    channel = RecordingChannel()
    _publish_order_confirmed(channel, routing_key="OrderConfirmed", delivery_mode=1)
    with pytest.raises(ContractError) as error:
        assert_valid_publish("checkout-service", channel.published[0])
    assert {p.code for p in error.value.problems} == {"routing_key", "delivery_mode", "confirms"}
    assert "routing_key" in str(error.value) and "delivery_mode" in str(error.value)


def test_queue_declare_must_be_passive():
    channel = RecordingChannel()
    channel.queue_declare("checkout-service.EventCancelled", passive=True)
    assert not channel.violations
    channel.queue_declare("checkout-service.EventCancelled", durable=True)
    assert [v.code for v in channel.violations] == ["queue_declare"]


@pytest.mark.parametrize(
    ("kwargs", "ok"),
    [
        ({"exchange": "checkout.events", "exchange_type": "topic", "durable": True}, True),
        ({"exchange": "checkout.events", "exchange_type": "topic", "passive": True}, True),
        ({"exchange": "event.events", "exchange_type": "topic", "durable": True}, False),
        ({"exchange": "checkout.events", "exchange_type": "direct", "durable": True}, False),
        ({"exchange": "checkout.events", "exchange_type": "topic"}, False),
        (
            {
                "exchange": "checkout.events",
                "exchange_type": "topic",
                "durable": True,
                "arguments": {"alternate-exchange": "x"},
            },
            False,
        ),
    ],
)
def test_exchange_declare_must_match_the_documented_one(kwargs, ok):
    channel = RecordingChannel(service="checkout-service")
    channel.exchange_declare(**kwargs)
    assert (not channel.violations) == ok


def test_properties_dict_accepts_mappings_objects_and_none():
    assert properties_dict(None) == {}
    assert properties_dict({"type": "X", "headers": {}}) == {"type": "X"}
    assert properties_dict(Props(type="X", delivery_mode=DeliveryMode.PERSISTENT)) == {
        "type": "X",
        "delivery_mode": 2,
    }


# ---------- listeners ----------


def test_assert_listener_queues():
    assert_listener_queues(
        "user-service", ["user-service.OrderConfirmed", "user-service.EventCancelled"]
    )
    with pytest.raises(ContractError, match="user-service.EventCancelled is not listened on"):
        assert_listener_queues("user-service", ["user-service.OrderConfirmed"])
