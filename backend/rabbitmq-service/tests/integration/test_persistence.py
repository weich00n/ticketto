"""Messages survive a broker restart and a redeploy, even if a publisher forgets to mark them
persistent. These tests start their own brokers so restarts don't disturb other tests."""

import uuid

import docker
import pytest

from tests.integration.conftest import (
    ADMIN_USER,
    CATALOGUE,
    QUEUE_NAMES,
    message_counts,
    publish,
    start_broker,
    wait_for,
)
from tools import generate_definitions as gen

pytestmark = pytest.mark.integration


def _publish_samples(broker):
    with broker.connect() as connection:
        ch = connection.channel()
        ch.confirm_delivery()
        publish(ch, "EventPublished")  # persistent, to every EventPublished consumer queue
        publish(ch, "OrderConfirmed", persistent=False)  # a publisher's mistake: transient
        publish(ch, "TicketIssued", routing_key="ticket.no_queue")  # held in ticketto.unrouted


# One message in each consumer queue of the two routed messages, read from the catalogue so
# adding a consumer in messages.yaml doesn't break this test.
EXPECTED_AFTER = (
    dict.fromkeys(QUEUE_NAMES, 0)
    | {
        queue.name: 1
        for queue in CATALOGUE.consumer_queues()
        if queue.message.name in ("EventPublished", "OrderConfirmed")
    }
    | {gen.UNROUTED: 1}
)


def _counts(broker):
    with broker.connect() as connection:
        return message_counts(connection.channel())


def test_messages_survive_a_restart(broker_image):
    broker = start_broker(broker_image)
    try:
        _publish_samples(broker)
        assert wait_for(lambda: _counts(broker) == EXPECTED_AFTER, what="messages published")

        broker.restart()

        # Quorum queues write every message to disk, transient ones included. Each queue
        # recovers its log in its own time after boot, so wait rather than read once.
        assert wait_for(lambda: _counts(broker) == EXPECTED_AFTER, timeout=30, what="recovery")
        code, output = broker.exec("ticketto-check")
        assert code == 0, output
    finally:
        broker.container.stop()


def test_messages_and_topology_survive_a_redeploy_with_a_rotated_password(broker_image):
    # What the EC2 deploy does: remove the container, then run the (new) image again with
    # the same hostname and the same data volume. The password is rotated at the same time.
    volume = f"ticketto-test-{uuid.uuid4().hex[:8]}"
    first = start_broker(broker_image, password="first-password", volume=volume)
    try:
        _publish_samples(first)
        assert wait_for(lambda: _counts(first) == EXPECTED_AFTER, what="messages published")
    finally:
        # Stop gracefully first, as deploy/run-rabbitmq.sh does. A SIGKILL within seconds of
        # a queue being created can lose that queue's registration, and with it the messages
        # (measured on 4.3.6: ticketto.unrouted, the last queue created at boot, lost them in
        # 4 of 10 runs). After a graceful stop, or a SIGKILL 30 s after boot, nothing was lost.
        first.container.get_wrapped_container().stop(timeout=60)
        first.container.stop()

    second = start_broker(broker_image, password="rotated-password", volume=volume)
    try:
        assert wait_for(lambda: _counts(second) == EXPECTED_AFTER, timeout=30, what="recovery")
        with pytest.raises(Exception, match="ACCESS_REFUSED|Authentication"):
            second.connect(ADMIN_USER, "first-password")
        code, output = second.exec("ticketto-check")
        assert code == 0, output
    finally:
        second.container.stop()
        docker.from_env().volumes.get(volume).remove(force=True)
