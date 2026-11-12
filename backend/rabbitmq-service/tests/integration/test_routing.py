"""Every catalogued message reaches exactly the queues messages.yaml says, and nothing else.

The expectations are read from topology/messages.yaml, so adding a consumer there adds a
case here automatically.
"""

import json

import pytest

from tests.integration.conftest import CATALOGUE, QUEUE_NAMES, message_counts, publish, wait_for
from tools import generate_definitions as gen

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("empty_queues")]


@pytest.mark.parametrize("message", CATALOGUE.messages, ids=lambda m: m.name)
def test_message_reaches_exactly_its_consumers(channel, message):
    publish(channel, message.name)

    expected = dict.fromkeys(QUEUE_NAMES, 0)
    for consumer in message.consumers:
        expected[f"{consumer}.{message.name}"] = 1
    if not message.consumers:
        expected[gen.UNROUTED] = 1  # kept until a consumer is added, never lost

    assert wait_for(lambda: message_counts(channel) == expected, what=message.name)


def test_unknown_routing_key_is_kept_in_unrouted(channel):
    # e.g. a typo in a publisher. Without the alternate exchange, event-service's mandatory
    # publish would be returned and retried forever.
    publish(channel, "EventPublished", routing_key="event.publishd")
    expected = dict.fromkeys(QUEUE_NAMES, 0) | {gen.UNROUTED: 1}
    assert wait_for(lambda: message_counts(channel) == expected, what="unrouted")


def test_unrouted_messages_keep_their_routing_key(channel):
    publish(
        channel,
        "TicketIssued",
        routing_key="ticket.no_queue",
        message_id="11111111-1111-4111-8111-111111111111",
    )
    method, properties, _ = wait_for(lambda: _get(channel, gen.UNROUTED), what="unrouted message")
    assert method.routing_key == "ticket.no_queue"
    assert method.exchange == "ticket.events"
    assert properties.message_id == "11111111-1111-4111-8111-111111111111"
    channel.basic_ack(method.delivery_tag)


def test_message_body_and_properties_arrive_unchanged(channel):
    body = json.dumps(
        {
            "message_id": "22222222-2222-4222-8222-222222222222",
            "message_type": "EventCancelled",
            "producer": "event-service",
            "payload": {"event_id": "a14ec682-62f5-46db-b94a-a8d95435e274"},
        }
    ).encode()
    publish(channel, "EventCancelled", body, message_id="22222222-2222-4222-8222-222222222222")

    for queue in ("reservation-service.EventCancelled", "checkout-service.EventCancelled"):
        method, properties, received = wait_for(
            lambda q=queue: _get(channel, q), what=f"message on {queue}"
        )
        assert received == body
        assert properties.message_id == "22222222-2222-4222-8222-222222222222"
        assert properties.correlation_id == "corr-1"
        assert properties.type == "EventCancelled"
        assert properties.content_type == "application/json"
        assert properties.delivery_mode == 2
        assert method.routing_key == "event.cancelled"
        channel.basic_ack(method.delivery_tag)


def test_message_names_are_not_routing_keys(channel):
    # The PascalCase name goes in the AMQP type property, not the routing key.
    publish(channel, "EventPublished", routing_key="EventPublished")
    expected = dict.fromkeys(QUEUE_NAMES, 0) | {gen.UNROUTED: 1}
    assert wait_for(lambda: message_counts(channel) == expected, what="unrouted")


def test_a_message_on_another_publishers_exchange_is_not_routed(channel):
    # Bindings are per exchange: event.published only routes from event.events.
    publish(channel, "EventPublished", exchange="checkout.events")
    expected = dict.fromkeys(QUEUE_NAMES, 0) | {gen.UNROUTED: 1}
    assert wait_for(lambda: message_counts(channel) == expected, what="unrouted")


def _get(ch, queue):
    """One message from queue (manual ack), or None if it is empty."""
    method, properties, body = ch.basic_get(queue, auto_ack=False)
    return (method, properties, body) if method else None
