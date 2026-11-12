"""Each service's own user can do its job and nothing else, enforced by the real broker.

reservation-service has its own password here (as on AWS); the others use the default one.
"""

import pika
import pytest
import requests

from tests.integration.conftest import (
    RESERVATION_PASS,
    SERVICE_PASS_DEFAULT,
    message_counts,
    publish,
    wait_for,
)
from tools import generate_definitions as gen

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("empty_queues")]

CLOSED = pika.exceptions.ChannelClosedByBroker
RESERVATION = ("reservation-service", RESERVATION_PASS)


def _channel(connection):
    ch = connection.channel()
    ch.confirm_delivery()
    return ch


def _refused(error) -> bool:
    return error.value.reply_code == 403  # ACCESS_REFUSED


def test_each_service_logs_in_with_its_own_password(broker):
    with broker.connect(*RESERVATION) as connection:
        assert connection.is_open
    with broker.connect("queue-service", SERVICE_PASS_DEFAULT) as connection:
        assert connection.is_open
    # The default password doesn't work for a service that has its own.
    with pytest.raises(pika.exceptions.ProbableAuthenticationError):
        broker.connect("reservation-service", SERVICE_PASS_DEFAULT)


def test_consumes_and_acks_from_its_own_queue(broker, channel):
    publish(channel, "EventPublished")
    queue = "reservation-service.EventPublished"
    with broker.connect(*RESERVATION) as connection:
        ch = connection.channel()
        assert ch.queue_declare(queue, passive=True).method.queue == queue
        method = wait_for(lambda: ch.basic_get(queue, auto_ack=False)[0], what="the message")
        ch.basic_ack(method.delivery_tag)
    assert message_counts(channel)[queue] == 0


@pytest.mark.parametrize(
    "queue",
    ["checkout-service.EventCancelled", "reservation-service.EventPublished.dlq"],
    ids=["another service's queue", "its own DLQ"],
)
def test_cannot_read_other_queues(broker, queue):
    with broker.connect(*RESERVATION) as connection:
        with pytest.raises(CLOSED) as error:
            connection.channel().basic_get(queue, auto_ack=False)
        assert _refused(error)


def test_cannot_create_queues(broker):
    with broker.connect(*RESERVATION) as connection:
        with pytest.raises(CLOSED) as error:
            connection.channel().queue_declare("reservation-service.Scratch", durable=True)
        assert _refused(error)


def test_may_declare_its_own_exchange_as_defined(broker):
    with broker.connect(*RESERVATION) as connection:
        ch = connection.channel()
        ch.exchange_declare("reservation.events", exchange_type="topic", durable=True)
        assert ch.is_open


def test_cannot_declare_another_services_exchange(broker):
    with broker.connect(*RESERVATION) as connection:
        with pytest.raises(CLOSED) as error:
            connection.channel().exchange_declare("event.events", "topic", durable=True)
        assert _refused(error)


def test_publishes_its_own_messages(broker, channel):
    with broker.connect(*RESERVATION) as connection:
        publish(_channel(connection), "RefundRequired")
    assert wait_for(
        lambda: message_counts(channel)["checkout-service.RefundRequired"] == 1, what="delivery"
    )


@pytest.mark.parametrize(
    ("exchange", "routing_key"),
    [("event.events", "event.published"), ("reservation.events", "order.confirmed")],
    ids=["another service's exchange", "another service's routing key"],
)
def test_cannot_publish_as_another_service(broker, channel, exchange, routing_key):
    with broker.connect(*RESERVATION) as connection:
        with pytest.raises(CLOSED) as error:
            publish(
                _channel(connection), "EventPublished", exchange=exchange, routing_key=routing_key
            )
        assert _refused(error)
    assert not any(message_counts(channel).values())  # nothing was delivered anywhere


def test_a_consumer_only_service_cannot_publish(broker):
    with broker.connect("queue-service", SERVICE_PASS_DEFAULT) as connection:
        with pytest.raises(CLOSED) as error:
            publish(_channel(connection), "EventPublished")
        assert _refused(error)


def test_service_users_cannot_use_the_management_api(broker):
    response = requests.get(broker.api_url("overview"), auth=RESERVATION, timeout=10)
    assert response.status_code == 401


@pytest.mark.parametrize("user", gen.service_users(gen.load_catalogue()), ids=lambda u: u.name)
def test_every_service_user_exists_without_tags(broker, user):
    assert broker.api_json(f"users/{user.name}")["tags"] == []
