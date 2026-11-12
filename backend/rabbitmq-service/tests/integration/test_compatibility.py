"""What existing clients do still works, and what docs/MESSAGING.md forbids fails loudly."""

import pika
import pytest

from tests.integration.conftest import CATALOGUE, message_counts, publish
from tools import generate_definitions as gen

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("empty_queues")]

CLOSED = pika.exceptions.ChannelClosedByBroker


@pytest.mark.parametrize("exchange", CATALOGUE.exchanges())
def test_publisher_exchange_declare_matches(broker, exchange):
    # Exactly what event-service's outbox worker does on every cycle (app/outbox.py), and what
    # queue-service does for every exchange it consumes from (app/messaging/topology.py).
    # Declaring an existing exchange with the same settings is a no-op: nothing new is created.
    with broker.connect() as connection:
        ch = connection.channel()
        ch.exchange_declare(exchange=exchange, exchange_type="topic", durable=True)
        ch.confirm_delivery()
        assert ch.is_open


def test_declaring_the_exchange_differently_is_refused(broker):
    # Why our definitions must match the publishers' declares exactly.
    with broker.connect() as connection:
        ch = connection.channel()
        with pytest.raises(CLOSED) as error:
            ch.exchange_declare(exchange="event.events", exchange_type="direct", durable=True)
        assert error.value.reply_code == 406  # PRECONDITION_FAILED


def test_exclusive_temporary_queue_still_works(channel):
    # event-service's integration tests bind a private, server-named queue.
    queue = channel.queue_declare(queue="", exclusive=True).method.queue
    channel.queue_bind(queue=queue, exchange="event.events", routing_key="event.published")
    publish(channel, "EventPublished")
    method, _, _ = channel.basic_get(queue, auto_ack=False)
    assert method is not None
    channel.basic_ack(method.delivery_tag)


def test_consumers_may_passive_declare_their_queue(broker):
    with broker.connect() as connection:
        ok = connection.channel().queue_declare("reservation-service.EventPublished", passive=True)
        assert ok.method.queue == "reservation-service.EventPublished"


def test_passive_declare_of_a_missing_queue_fails_with_404(broker):
    with broker.connect() as connection:
        with pytest.raises(CLOSED) as error:
            connection.channel().queue_declare("reservation-service.NoSuchMessage", passive=True)
        assert error.value.reply_code == 404


def test_redeclaring_a_queue_with_other_arguments_is_refused(broker):
    # A consumer that declares its own queue (classic, no DLX) gets 406 and loses its channel.
    with broker.connect() as connection:
        with pytest.raises(CLOSED) as error:
            connection.channel().queue_declare("reservation-service.EventPublished", durable=True)
        assert error.value.reply_code == 406


def test_checkout_services_current_publisher_loses_messages(channel):
    # checkout-service (app/messaging.py) today declares its own `orders.topic` exchange and
    # publishes without `mandatory` or confirms. Nothing is bound to orders.topic, so the
    # broker accepts the message and drops it: no consumer queue and not even
    # ticketto.unrouted ever sees it. This is why checkout must move to checkout.events.
    channel.exchange_declare(exchange="orders.topic", exchange_type="topic", durable=True)
    try:
        channel.basic_publish(
            exchange="orders.topic",
            routing_key="OrderConfirmed",
            body=b"{}",
            properties=pika.BasicProperties(delivery_mode=2),
        )
        counts = message_counts(channel)
        assert counts["ticket-service.OrderConfirmed"] == 0
        assert counts["queue-service.OrderConfirmed"] == 0
        assert counts[gen.UNROUTED] == 0
    finally:
        channel.exchange_delete("orders.topic")


def test_dlx_routes_only_by_queue_name(channel):
    # ticketto.dlx is keyed by the failed queue's name; a message's routing key matches nothing.
    with pytest.raises(pika.exceptions.UnroutableError):
        publish(channel, "EventPublished", exchange=gen.DEAD_LETTER_EXCHANGE)
