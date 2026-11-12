"""Draining the queues with basic_get, and one whole run. Tests 28-32 of the plan."""

import pytest

from app.broker import QUEUES, Disposition, dispose, drain
from app.email_delivery import utcnow
from app.models import STATUS_SENT, Notification
from app.run import run_once
from tests.unit.conftest import (
    EVENT,
    OTHER_EVENT,
    FakeChannel,
    FakeClock,
    envelope,
    event_cancelled,
    event_published,
    notifications,
    order_refunded,
    refund_failed,
    sub,
    ticket_issued,
    user,
)

PUBLISHED_Q, CANCELLED_Q, REFUNDED_Q, REFUND_FAILED_Q, TICKET_ISSUED_Q = (
    f"notification-service.{message}"
    for message in (
        "EventPublished",
        "EventCancelled",
        "OrderRefunded",
        "RefundFailed",
        "TicketIssued",
    )
)


def test_queue_names_match_the_broker_topology():
    assert QUEUES == (PUBLISHED_Q, CANCELLED_Q, REFUNDED_Q, REFUND_FAILED_Q, TICKET_ISSUED_Q)


def test_dispositions():
    def ok(_message):
        return "done"

    def broken(_message):
        raise RuntimeError("db down")

    assert dispose(event_published(), ok) is Disposition.ACK
    assert dispose(event_published(), broken) is Disposition.REQUEUE
    assert dispose(b"{", ok) is Disposition.DEAD_LETTER
    assert dispose(envelope("PaymentCompleted", {}), broken) is Disposition.ACK  # not ours


def test_drains_until_empty_acking_each_after_handling():
    channel = FakeChannel({PUBLISHED_Q: [event_published(), event_published()]})
    handled = []
    result = drain(channel, PUBLISHED_Q, handled.append, deadline=float("inf"))
    assert (result.acked, result.dead_lettered, result.requeued) == (2, 0, 0)
    assert len(handled) == 2
    assert channel.calls == [
        ("get", PUBLISHED_Q),
        ("ack", 1),
        ("get", PUBLISHED_Q),
        ("ack", 2),
        ("get", PUBLISHED_Q),
    ]


def test_poison_is_dead_lettered_and_draining_continues():
    channel = FakeChannel({PUBLISHED_Q: [b"not json", event_published()]})
    result = drain(channel, PUBLISHED_Q, lambda m: None, deadline=float("inf"))
    assert (result.acked, result.dead_lettered) == (1, 1)
    assert ("reject", 1, False) in channel.calls


def test_transient_error_requeues_and_stops_that_queue():
    channel = FakeChannel({PUBLISHED_Q: [event_published(), event_published()]})

    def broken(_message):
        raise RuntimeError("user-service down")

    result = drain(channel, PUBLISHED_Q, broken, deadline=float("inf"))
    assert (result.acked, result.requeued) == (0, 1)
    # Stopped after one: retrying at once would use up the message's 5 deliveries in seconds.
    assert channel.calls == [("get", PUBLISHED_Q), ("reject", 1, True)]
    assert len(channel.queues[PUBLISHED_Q]) == 2


def test_stops_taking_messages_at_the_deadline():
    channel = FakeChannel({PUBLISHED_Q: [event_published() for _ in range(5)]})
    clock = FakeClock(start=0, step=1)
    result = drain(channel, PUBLISHED_Q, lambda m: None, deadline=2, clock=clock)
    assert 0 < result.acked < 5
    assert len(channel.queues[PUBLISHED_Q]) == 5 - result.acked


def test_only_basic_get_ack_and_reject_are_used():
    # FakeChannel has no queue_declare, basic_consume or basic_nack: using one would raise.
    channel = FakeChannel({PUBLISHED_Q: [b"{", event_published()]})

    seen = []

    def flaky(message):
        seen.append(message)
        if len(seen) == 1:
            raise RuntimeError("once")

    drain(channel, PUBLISHED_Q, flaky, deadline=float("inf"))
    assert {call[0] for call in channel.calls} <= {"get", "ack", "reject"}


# ---------- one whole run ----------


class FakeConnection:
    def __init__(self, channel):
        self._channel = channel
        self.closed = False

    def channel(self):
        return self._channel

    def close(self):
        self.closed = True


@pytest.fixture
def run(settings, session_factory, users, events, sender):
    """run_once with the fakes, connected to `channel`."""

    def _run(channel, **kwargs):
        connection = FakeConnection(channel)
        result = run_once(
            settings=settings,
            session_factory=session_factory,
            users=users,
            events=events,
            sender=sender,
            connect=lambda url: connection,
            deadline=kwargs.pop("deadline", float("inf")),
            **kwargs,
        )
        assert connection.closed
        return result

    return _run


def test_run_drains_every_queue_then_sends(run, users, sender):
    users.opted_in_users = [user(1)]
    users.buyers_by_event = {EVENT: [user(2)]}
    users.users_by_sub = {sub(3): user(3)}
    channel = FakeChannel(
        {
            PUBLISHED_Q: [event_published()],
            CANCELLED_Q: [event_cancelled()],
            REFUNDED_Q: [order_refunded(sub(3))],
            REFUND_FAILED_Q: [refund_failed(sub(3))],
            TICKET_ISSUED_Q: [ticket_issued(sub(4), OTHER_EVENT)],
        }
    )

    result = run(channel)

    assert result["drained"] == {
        PUBLISHED_Q: 1,
        CANCELLED_Q: 1,
        REFUNDED_Q: 1,
        REFUND_FAILED_Q: 1,
        TICKET_ISSUED_Q: 1,
    }
    # The new-event email was cancelled by the cancellation before it could be sent.
    assert result["sent"] == 4
    assert sorted(e.to for e in sender.sent) == [
        "buyer@example.com",
        user(2).email,
        user(3).email,
        user(3).email,
    ]


def test_one_failing_queue_doesnt_stop_the_others(run, users, sender):
    users.opted_in_users = [user(1)]
    channel = FakeChannel(
        {
            PUBLISHED_Q: [event_published()],
            REFUNDED_Q: [order_refunded(sub(9)), order_refunded(sub(9))],
        }
    )
    result = run(channel)
    assert result["drained"][PUBLISHED_Q] == 1
    assert result["requeued"] == {REFUNDED_Q: 1}  # unknown sub: back to the queue, then stop
    assert len(channel.queues[REFUNDED_Q]) == 2
    assert [e.to for e in sender.sent] == [user(1).email]


def test_a_quiet_run_has_nothing_more_to_do(run):
    assert run(FakeChannel({}))["more_to_do"] is False


def test_a_run_that_empties_the_queues_has_nothing_more_to_do(run, users):
    users.opted_in_users = [user(1)]
    assert run(FakeChannel({PUBLISHED_Q: [event_published()]}))["more_to_do"] is False


def test_draining_gets_half_the_time_then_emails_still_go_out(run, session_factory, users, sender):
    with session_factory() as session, session.begin():
        session.add(
            Notification(
                kind="EVENT_PUBLISHED",
                dedupe_key="already-queued",
                source_message_id="m",
                event_id=EVENT,
                user_id="u",
                recipient="waiting@example.com",
                context={"name": "W", "title": "T", "starts_at": None},
                status="PENDING",
                attempts=0,
                next_attempt_at=utcnow(),
            )
        )
    users.opted_in_users = [user(1)]
    channel = FakeChannel(
        {PUBLISHED_Q: [event_published(f"{n:08d}-0000-4000-8000-000000000000") for n in range(50)]}
    )
    clock = FakeClock(start=0, step=1)  # each clock read is a second of work

    result = run(channel, deadline=40, clock=clock)

    assert 0 < result["drained"][PUBLISHED_Q] < 50  # stopped at about 20 s
    assert len(channel.queues[PUBLISHED_Q]) == 50 - result["drained"][PUBLISHED_Q]
    assert result["sent"] >= 1  # the sending step still ran
    assert "waiting@example.com" in [e.to for e in sender.sent]
    assert result["more_to_do"] is True  # the rest are still on the queue


def test_broker_down_still_sends_queued_emails(settings, session_factory, users, events, sender):
    with session_factory() as session, session.begin():
        session.add(
            Notification(
                kind="EVENT_CANCELLED",
                dedupe_key="k",
                source_message_id="m",
                event_id=EVENT,
                user_id="u",
                recipient="buyer@example.com",
                context={"name": "B", "title": "T", "starts_at": None, "reason": "r"},
                status="PENDING",
                attempts=0,
                next_attempt_at=utcnow(),
            )
        )

    def refuse(_url):
        raise OSError("connection refused")

    result = run_once(
        settings=settings,
        session_factory=session_factory,
        users=users,
        events=events,
        sender=sender,
        connect=refuse,
        deadline=float("inf"),
    )
    assert result["broker_error"] == "OSError: connection refused"
    assert result["sent"] == 1
    assert notifications(session_factory)[0].status == STATUS_SENT


@pytest.mark.parametrize("fraction", [0.0, 1.5])
def test_drain_fraction_must_be_between_0_and_1(settings, fraction):
    from app.config import ConfigError, Settings  # pylint: disable=import-outside-toplevel

    with pytest.raises(ConfigError):
        Settings(**{**settings.__dict__, "drain_budget_fraction": fraction})
