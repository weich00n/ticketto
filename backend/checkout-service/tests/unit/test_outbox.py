import json
import logging
from pathlib import Path

import pytest
from pika.exceptions import NackError, StreamLostError, UnroutableError
from sqlalchemy import select

from app.messaging.events import ROUTING_KEYS
from app.messaging.outbox import EXCHANGE, MAX_ATTEMPTS, Outbox, build_envelope, message_id_for
from app.models import OutboxMessage
from tests.conftest import FakeOutbox


def test_build_envelope_has_exactly_the_broker_fields():
    env = build_envelope("OrderConfirmed", {"order_id": "o1"})
    assert set(env) == {
        "message_id",
        "message_type",
        "schema_version",
        "occurred_at",
        "producer",
        "payload",
    }
    assert env["message_type"] == "OrderConfirmed"
    assert env["schema_version"] == 1
    assert env["producer"] == "checkout-service"
    assert env["payload"] == {"order_id": "o1"}
    # occurred_at is ISO 8601 UTC with a Z suffix
    assert env["occurred_at"].endswith("Z")
    # message_id is a lowercase uuid
    assert env["message_id"] == env["message_id"].lower()


def test_routing_keys_match_the_broker_catalogue():
    # rabbitmq-service topology/messages.yaml: dotted lower case, on checkout.events.
    assert EXCHANGE == "checkout.events"
    assert ROUTING_KEYS == {
        "OrderConfirmed": "order.confirmed",
        "OrderRefunded": "order.refunded",
        "RefundCompleted": "refund.completed",
        "RefundFailed": "refund.failed",
    }


def test_ci_topology_routes_checkout_queues_from_the_publishers_exchanges():
    path = Path(__file__).parents[1] / "integration" / "checkout-topology.json"
    topology = json.loads(path.read_text())
    bindings = {
        (b["source"], b["routing_key"], b["destination"])
        for b in topology["bindings"]
        if b["source"] != "ticketto.dlx"
    }
    assert ("event.events", "event.cancelled", "checkout-service.EventCancelled") in bindings
    assert ("reservation.events", "refund.required", "checkout-service.RefundRequired") in bindings
    assert (
        "reservation.events",
        "reservation.expired",
        "checkout-service.ReservationExpired",
    ) in bindings
    assert EXCHANGE in {e["name"] for e in topology["exchanges"]}


def test_write_then_deliver_publishes_in_commit_order(settings, session_factory):
    outbox = FakeOutbox(settings)
    with session_factory() as db:
        outbox.write(
            db, message_type="OrderConfirmed", payload={"order_id": "o1"}, aggregate_id="o1"
        )
        outbox.write(
            db, message_type="RefundCompleted", payload={"refund_id": "r1"}, aggregate_id="r1"
        )
        db.commit()
    with session_factory() as db:
        sent = outbox.deliver_pending(db)
    assert sent == 2
    assert [rk for rk, _ in outbox.published] == ["order.confirmed", "refund.completed"]
    # The stored payload is the full envelope, publishable unchanged.
    assert outbox.published[-1][1]["message_type"] == "RefundCompleted"


def test_delivered_message_not_republished(settings, session_factory):
    outbox = FakeOutbox(settings)
    with session_factory() as db:
        outbox.write(db, message_type="OrderConfirmed", payload={}, aggregate_id="o1")
        db.commit()
    with session_factory() as db:
        assert outbox.deliver_pending(db) == 1
        assert outbox.deliver_pending(db) == 0  # already delivered


def test_same_fact_written_twice_is_one_message(settings, session_factory):
    outbox = FakeOutbox(settings)
    with session_factory() as db:
        first = outbox.write(db, message_type="RefundFailed", payload={}, aggregate_id="r1")
        db.commit()
        again = outbox.write(db, message_type="RefundFailed", payload={}, aggregate_id="r1")
        db.commit()
        assert again.id == first.id
        assert db.query(OutboxMessage).count() == 1
    assert first.message_id == message_id_for("RefundFailed", "r1")
    # A different type about the same aggregate is a different message.
    assert message_id_for("RefundCompleted", "r1") != first.message_id


def test_unknown_message_type_has_no_routing_key(settings, session_factory):
    outbox = FakeOutbox(settings)
    with session_factory() as db, pytest.raises(KeyError):
        outbox.write(db, message_type="Nope", payload={}, aggregate_id="o1")


# ------------------------------------------------- the real relay, against a fake channel


class _FakeChannel:
    """Confirms every publish except those `refuse` picks, which raise `error`."""

    def __init__(self, refuse=lambda _message_id: False, error: Exception | None = None):
        self.refuse = refuse
        self.error = error or NackError([])
        self.sent: list[str] = []  # message ids, in publish order

    def confirm_delivery(self):
        pass

    def basic_publish(self, *, properties, **_kwargs):
        if self.refuse(properties.message_id):
            raise self.error
        self.sent.append(properties.message_id)


class _FakeConnection:
    def __init__(self, channel):
        self._channel = channel
        self.is_open = True

    def channel(self):
        return self._channel

    def close(self):
        self.is_open = False


def _relay(settings, channel) -> Outbox:
    relay = Outbox(settings)
    relay._connect = lambda: _FakeConnection(channel)  # pylint: disable=protected-access
    return relay


def _stage(settings, session_factory, *facts: tuple[str, str]) -> list[str]:
    """Write (message type, aggregate) facts in order; returns their message ids."""
    with session_factory() as db:
        ids = [
            Outbox(settings)
            .write(db, message_type=message_type, payload={}, aggregate_id=aggregate)
            .message_id
            for message_type, aggregate in facts
        ]
        db.commit()
    return ids


def _row(session_factory, message_id: str) -> OutboxMessage:
    with session_factory() as db:
        return db.scalar(select(OutboxMessage).where(OutboxMessage.message_id == message_id))


@pytest.mark.parametrize(
    "error", [NackError([]), UnroutableError([]), TypeError("not serialisable")]
)
def test_a_refused_message_is_skipped_so_the_ones_behind_it_still_go(
    settings, session_factory, error
):
    bad, good = _stage(
        settings, session_factory, ("OrderConfirmed", "o1"), ("OrderConfirmed", "o2")
    )
    channel = _FakeChannel(refuse=lambda message_id: message_id == bad, error=error)

    with session_factory() as db:
        assert _relay(settings, channel).deliver_pending(db) == 1

    assert channel.sent == [good]
    assert _row(session_factory, bad).delivered_at is None
    assert _row(session_factory, bad).attempts == 1
    assert _row(session_factory, bad).last_error.startswith(type(error).__name__)
    assert _row(session_factory, good).delivered_at is not None


def test_later_messages_about_the_same_aggregate_wait_behind_a_refused_one(
    settings, session_factory
):
    bad, same_order, other_order = _stage(
        settings,
        session_factory,
        ("OrderConfirmed", "o1"),
        ("OrderRefunded", "o1"),
        ("OrderConfirmed", "o2"),
    )
    channel = _FakeChannel(refuse=lambda message_id: message_id == bad)
    with session_factory() as db:
        _relay(settings, channel).deliver_pending(db)
    assert channel.sent == [other_order]
    assert _row(session_factory, same_order).attempts == 0

    channel.refuse = lambda _message_id: False
    with session_factory() as db:
        assert _relay(settings, channel).deliver_pending(db) == 2
    assert channel.sent == [other_order, bad, same_order]


def test_a_message_refused_max_attempts_times_is_given_up_with_an_error(
    settings, session_factory, caplog
):
    bad, behind = _stage(
        settings, session_factory, ("OrderConfirmed", "o1"), ("OrderRefunded", "o1")
    )
    channel = _FakeChannel(refuse=lambda message_id: message_id == bad)
    relay = _relay(settings, channel)

    for _ in range(MAX_ATTEMPTS):
        with session_factory() as db:
            relay.deliver_pending(db)
    assert _row(session_factory, bad).attempts == MAX_ATTEMPTS
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].message_id == bad

    # No longer picked up, so the message behind it goes out.
    with session_factory() as db:
        assert relay.deliver_pending(db) == 1
    assert channel.sent == [behind]
    assert _row(session_factory, bad).attempts == MAX_ATTEMPTS
    assert _row(session_factory, bad).delivered_at is None


def test_an_unavailable_broker_counts_no_attempt_and_ends_the_cycle(settings, session_factory):
    first, second = _stage(
        settings, session_factory, ("OrderConfirmed", "o1"), ("OrderConfirmed", "o2")
    )
    channel = _FakeChannel(refuse=lambda _message_id: True, error=StreamLostError("gone"))

    for _ in range(MAX_ATTEMPTS + 1):
        with session_factory() as db:
            assert _relay(settings, channel).deliver_pending(db) == 0

    # An outage doesn't use up the attempts, so the messages still go once it is over.
    assert _row(session_factory, first).attempts == 0
    assert _row(session_factory, first).last_error.startswith("StreamLostError")
    assert _row(session_factory, second).last_error is None
    channel.refuse = lambda _message_id: False
    with session_factory() as db:
        assert _relay(settings, channel).deliver_pending(db) == 2
    assert channel.sent == [first, second]
