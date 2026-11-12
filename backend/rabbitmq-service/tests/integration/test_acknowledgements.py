"""Manual acks keep messages safe, and ticketto-check catches consumers that use auto-ack."""

import pytest

from tests.integration.conftest import message_counts, publish, wait_for

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("empty_queues")]

QUEUE = "reservation-service.EventPublished"


def test_auto_ack_consumer_is_flagged(broker):
    with broker.connect() as connection:
        ch = connection.channel()
        ch.basic_consume(QUEUE, lambda *_: None, auto_ack=True, consumer_tag="careless")
        code, output = broker.exec("ticketto-check")
    assert code == 1
    assert f"consumer 'careless' on queue {QUEUE} uses auto-ack" in output


def test_deploy_health_check_ignores_consumers(broker):
    # The deploy's rollback decision: a service's auto-ack bug is not a reason to roll the
    # broker back. The full check still runs (and fails) afterwards.
    with broker.connect() as connection:
        ch = connection.channel()
        ch.basic_consume(QUEUE, lambda *_: None, auto_ack=True, consumer_tag="careless")
        code, output = broker.exec("ticketto-check", "--skip-consumers")
    assert code == 0, output
    assert "consumers not checked" in output


def test_manual_ack_consumer_passes(broker):
    with broker.connect() as connection:
        ch = connection.channel()
        ch.basic_qos(prefetch_count=10)
        ch.basic_consume(QUEUE, lambda *_: None, auto_ack=False, consumer_tag="careful")
        code, output = broker.exec("ticketto-check")
    assert code == 0, output


def test_unacked_message_is_redelivered_when_the_consumer_dies(broker, channel):
    publish(channel, "EventPublished")

    # A consumer takes the message and crashes before acking (its connection drops).
    with broker.connect() as crashing:
        crashing_channel = crashing.channel()
        method = wait_for(
            lambda: crashing_channel.basic_get(QUEUE, auto_ack=False)[0], what="the message"
        )
        assert method.redelivered is False

    # The broker hands it out again; nothing was lost.
    def redelivered():
        method, _, _ = channel.basic_get(QUEUE, auto_ack=False)
        return method

    method = wait_for(redelivered, what="the redelivery")
    assert method.redelivered is True
    channel.basic_ack(method.delivery_tag)
    assert message_counts(channel)[QUEUE] == 0


def test_auto_ack_loses_the_message_if_the_consumer_dies(broker, channel):
    # The reason for the rule: with auto-ack the broker deletes the message on delivery.
    publish(channel, "EventPublished")
    with broker.connect() as crashing:
        crashing_channel = crashing.channel()
        wait_for(lambda: crashing_channel.basic_get(QUEUE, auto_ack=True)[0], what="message")
    assert message_counts(channel)[QUEUE] == 0
