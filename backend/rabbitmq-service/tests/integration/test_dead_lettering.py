"""Failed messages end up in their queue's DLQ instead of being lost or blocking the queue."""

import pytest

from tests.integration.conftest import CATALOGUE, message_counts, publish, wait_for

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("empty_queues")]

QUEUE = "checkout-service.EventCancelled"
DLQ = f"{QUEUE}.dlq"
MESSAGE_ID = "33333333-3333-4333-8333-333333333333"


def _get(ch, queue):
    method, properties, body = ch.basic_get(queue, auto_ack=False)
    return (method, properties, body) if method else None


def test_rejected_message_goes_to_its_own_dlq(channel):
    publish(channel, "EventCancelled", b'{"poison": true}', message_id=MESSAGE_ID)
    method, _, _ = wait_for(lambda: _get(channel, QUEUE), what="the message")

    # A consumer that can never process this message rejects it without requeueing.
    channel.basic_nack(method.delivery_tag, requeue=False)

    method, properties, body = wait_for(lambda: _get(channel, DLQ), what="the dead letter")
    assert body == b'{"poison": true}'
    assert properties.message_id == MESSAGE_ID
    death = properties.headers["x-death"][0]
    assert death["queue"] == QUEUE
    assert death["reason"] == "rejected"
    assert properties.headers["x-first-death-exchange"] == "event.events"
    channel.basic_ack(method.delivery_tag)

    # Only this consumer's copy failed: the other consumers' copies are untouched.
    counts = message_counts(channel)
    assert counts["reservation-service.EventCancelled"] == 1
    assert counts["queue-service.EventCancelled"] == 1
    assert counts["reservation-service.EventCancelled.dlq"] == 0


def _reject_and_requeue(_broker, ch, method):
    ch.basic_reject(method.delivery_tag, requeue=True)


def _crash_holding_it(_broker, ch, _method):
    # The consumer dies (here: its channel closes) before acking; the broker takes it back.
    ch.close()


def _redeliveries_until_dead_lettered(broker, give_back, limit=25):
    """Keep failing the message the way give_back does; count deliveries until the broker
    dead-letters it (or `limit` is reached)."""
    with broker.connect() as connection:
        control = connection.channel()
        deliveries = 0
        while deliveries < limit:
            ch = connection.channel()

            def next_delivery(ch=ch):
                message = _get(ch, QUEUE)
                if message:
                    return message
                return "dead-lettered" if message_counts(control)[DLQ] == 1 else None

            outcome = wait_for(next_delivery, what="next attempt")
            if outcome == "dead-lettered":
                break
            deliveries += 1
            give_back(broker, ch, outcome[0])
        return deliveries


@pytest.mark.parametrize("give_back", [_reject_and_requeue, _crash_holding_it])
def test_poison_message_is_dead_lettered_after_the_delivery_limit(broker, channel, give_back):
    publish(channel, "EventCancelled", message_id=MESSAGE_ID)

    deliveries = _redeliveries_until_dead_lettered(broker, give_back)

    # The first delivery plus `delivery_limit` redeliveries, then it is moved aside.
    assert deliveries == CATALOGUE.delivery_limit + 1
    method, properties, _ = wait_for(lambda: _get(channel, DLQ), what="the dead letter")
    assert properties.headers["x-death"][0]["reason"] == "delivery_limit"
    assert properties.message_id == MESSAGE_ID
    channel.basic_ack(method.delivery_tag)
    assert message_counts(channel)[QUEUE] == 0  # the queue is unblocked


def test_nack_with_requeue_does_not_count_towards_the_limit(broker, channel):
    # RabbitMQ 4.3 (unlike 4.1) does not count basic.nack(requeue=True) as a failed delivery,
    # so a consumer that retries this way loops forever on a poison message. That is why
    # docs/MESSAGING.md says: to retry, basic_reject(requeue=True); never nack-requeue.
    # If a RabbitMQ upgrade changes this, this test fails and the docs need updating.
    publish(channel, "EventCancelled", message_id=MESSAGE_ID)

    def nack_and_requeue(_broker, ch, method):
        ch.basic_nack(method.delivery_tag, requeue=True)

    assert _redeliveries_until_dead_lettered(broker, nack_and_requeue, limit=15) == 15
    assert message_counts(channel)[DLQ] == 0


def test_dlq_messages_are_never_dropped_however_often_they_are_retried(channel):
    # Quorum queues drop a message after 20 failed deliveries by default, and a DLQ has no
    # DLX of its own, so the holding-cap policy turns the limit off (-1) for DLQs.
    publish(channel, "EventCancelled", message_id=MESSAGE_ID)
    method, _, _ = wait_for(lambda: _get(channel, QUEUE), what="the message")
    channel.basic_nack(method.delivery_tag, requeue=False)
    assert wait_for(lambda: message_counts(channel)[DLQ] == 1, what="dead letter")

    # e.g. someone replaying it from the DLQ whose consumer keeps failing.
    for _ in range(25):
        method, _, _ = wait_for(lambda: _get(channel, DLQ), what="dead letter")
        channel.basic_reject(method.delivery_tag, requeue=True)
    assert wait_for(lambda: message_counts(channel)[DLQ] == 1, what="dead letter kept")
