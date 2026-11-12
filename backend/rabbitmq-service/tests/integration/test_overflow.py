"""A full holding queue refuses new messages (the publisher is told) instead of dropping any."""

import pika
import pytest

from tests.integration.conftest import message_counts, publish, queue_path, wait_for
from tools import generate_definitions as gen

pytestmark = pytest.mark.integration

TEST_POLICY = "policies/%2F/test-tiny-cap"


@pytest.fixture()
def tiny_unrouted_cap(broker):
    """Shrink ticketto.unrouted to one message. A higher-priority policy replaces
    holding-cap for that queue, so it repeats the other settings."""
    broker.api(
        "PUT",
        TEST_POLICY,
        json={
            "pattern": r"^ticketto\.unrouted$",
            "apply-to": "queues",
            "priority": 10,
            "definition": {"max-length": 1, "overflow": "reject-publish", "delivery-limit": -1},
        },
    ).raise_for_status()
    wait_for(
        lambda: broker.api_json(queue_path(gen.UNROUTED)).get("policy") == "test-tiny-cap",
        what="the test policy to apply",
    )
    yield
    broker.api("DELETE", TEST_POLICY).raise_for_status()
    wait_for(
        lambda: broker.api_json(queue_path(gen.UNROUTED)).get("policy") == "holding-cap",
        what="holding-cap to apply again",
    )


@pytest.mark.usefixtures("empty_queues", "tiny_unrouted_cap")
def test_full_holding_queue_nacks_instead_of_dropping(channel):
    # Quorum queues check the limit just after enqueueing, so they overshoot it by one
    # message before refusing. Measured on 4.1 and 4.3: with max-length 1, two are accepted.
    accepted, refused = [], []
    for n in range(5):
        message_id = f"44444444-4444-4444-8444-44444444444{n}"
        try:
            publish(channel, "TicketIssued", routing_key="ticket.no_queue", message_id=message_id)
            accepted.append(message_id)
        except pika.exceptions.NackError:
            # The publisher is told it was not stored, so its outbox keeps it and retries.
            refused.append(message_id)

    assert len(accepted) == 2
    assert len(refused) == 3

    # Every accepted message is still there, in order: nothing was silently dropped.
    held = []
    while (got := channel.basic_get(gen.UNROUTED, auto_ack=False))[0]:
        held.append(got[1].message_id)
        channel.basic_ack(got[0].delivery_tag)
    assert held == accepted
    assert message_counts(channel)[gen.UNROUTED] == 0
