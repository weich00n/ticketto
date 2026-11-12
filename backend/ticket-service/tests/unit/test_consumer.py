import threading

import pika.exceptions
import pytest

from app import consumer as consumer_module
from app.consumer import (
    CONSUMER_QUEUES,
    QUEUE_MISSING_RETRY_SECONDS,
    Disposition,
    QueueConsumer,
    QueueUnavailable,
    check_queue,
    dispose,
    settle,
)
from tests.unit.conftest import envelope, new_id, order_payload


def test_successful_message_is_acked():
    seen = []

    assert dispose(envelope("OrderConfirmed", order_payload()), seen.append) is Disposition.ACK
    assert seen[0].message_type == "OrderConfirmed"


def test_poison_message_is_dead_lettered_without_running_the_handler():
    seen = []

    assert dispose(b"{not json", seen.append) is Disposition.DEAD_LETTER
    assert not seen


def test_unused_message_type_is_acked_without_running_the_handler():
    seen = []

    body = envelope("EventSoldOut", {"event_id": new_id()})

    assert dispose(body, seen.append) is Disposition.ACK
    assert not seen


def test_handler_failure_is_requeued():
    def fail(_message):
        raise ConnectionError("database restarting")

    assert dispose(envelope("OrderConfirmed", order_payload()), fail, attempt=2) is (
        Disposition.REQUEUE
    )


class RecordingChannel:
    """Records every channel method called on it; queue_declare can be told to fail."""

    def __init__(self, declare_error: Exception | None = None):
        self.calls = []
        self.declare_error = declare_error

    def queue_declare(self, *args, **kwargs):
        self.calls.append(("queue_declare", args, kwargs))
        if self.declare_error:
            raise self.declare_error

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.calls.append((name, args, kwargs))


def test_queue_names_are_the_brokers():
    # rabbitmq-service creates `<consumer>.<Message>` from its topology/messages.yaml.
    assert [q.name for q in CONSUMER_QUEUES] == [
        "ticket-service.OrderConfirmed",
        "ticket-service.EventCancelled",
        "ticket-service.RefundCompleted",
        "ticket-service.EventPublished",
    ]
    assert CONSUMER_QUEUES[0].dlq == "ticket-service.OrderConfirmed.dlq"


@pytest.mark.parametrize(
    "disposition, call, requeue",
    [
        (Disposition.ACK, "basic_ack", None),
        (Disposition.DEAD_LETTER, "basic_reject", False),
        (Disposition.REQUEUE, "basic_reject", True),
    ],
)
def test_settle_never_nacks(disposition, call, requeue):
    # On the broker's RabbitMQ a nack with requeue doesn't count towards the delivery limit.
    channel = RecordingChannel()

    settle(channel, 7, disposition)

    assert [(name, args) for name, args, _ in channel.calls] == [(call, (7,))]
    if requeue is not None:
        assert channel.calls[0][2] == {"requeue": requeue}


def test_check_queue_is_passive():
    channel = RecordingChannel()

    check_queue(channel, "ticket-service.OrderConfirmed")

    assert channel.calls == [
        ("queue_declare", ("ticket-service.OrderConfirmed",), {"passive": True})
    ]


@pytest.mark.parametrize("code", [404, 403])
def test_missing_or_forbidden_queue_is_reported(code):
    channel = RecordingChannel(pika.exceptions.ChannelClosedByBroker(code, "NOT_FOUND"))

    with pytest.raises(QueueUnavailable) as caught:
        check_queue(channel, "ticket-service.RefundCompleted")

    assert caught.value.reply_code == code


def test_other_channel_errors_are_not_hidden():
    channel = RecordingChannel(pika.exceptions.ChannelClosedByBroker(406, "PRECONDITION"))

    with pytest.raises(pika.exceptions.ChannelClosedByBroker):
        check_queue(channel, "ticket-service.OrderConfirmed")


class FakeConnection:
    def __init__(self, channel, stop):
        self._channel = channel
        self._stop = stop
        self.is_open = True

    def channel(self):
        return self._channel

    def process_data_events(self, **_kwargs):
        self._stop.set()  # one loop is enough

    def close(self):
        self.is_open = False


def test_consumer_only_consumes_and_never_declares(monkeypatch):
    stop = threading.Event()
    channel = RecordingChannel()
    monkeypatch.setattr(
        consumer_module.pika, "BlockingConnection", lambda _params: FakeConnection(channel, stop)
    )

    QueueConsumer(CONSUMER_QUEUES[0], "amqp://x", lambda _m: None, stop).run()

    names = [name for name, _, _ in channel.calls]
    assert names == ["queue_declare", "basic_qos", "add_on_cancel_callback", "basic_consume"]
    assert channel.calls[0][2] == {"passive": True}
    assert channel.calls[3][1] == ("ticket-service.OrderConfirmed",)
    assert channel.calls[3][2]["auto_ack"] is False


def test_consumer_cancelled_by_the_broker_is_treated_as_a_missing_queue(monkeypatch):
    stop = threading.Event()
    channel = RecordingChannel()
    connection = FakeConnection(channel, threading.Event())  # never stops by itself

    def cancel_on_first_poll(**_kwargs):  # the broker deletes the queue while we consume
        for name, args, _ in channel.calls:
            if name == "add_on_cancel_callback":
                args[0](object())

    connection.process_data_events = cancel_on_first_poll
    monkeypatch.setattr(consumer_module.pika, "BlockingConnection", lambda _params: connection)
    consumer = QueueConsumer(CONSUMER_QUEUES[0], "amqp://x", lambda _m: None, stop)

    with pytest.raises(QueueUnavailable):
        consumer._consume()  # pylint: disable=protected-access

    assert not connection.is_open


class RecordingStop:
    """A stop event that records how long the consumer waits, and stops it after one wait."""

    def __init__(self):
        self.waits = []
        self._set = False

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def wait(self, seconds):
        self.waits.append(seconds)
        self._set = True


def test_missing_queue_is_retried_later_instead_of_crashing(monkeypatch):
    stop = RecordingStop()
    consumer = QueueConsumer(CONSUMER_QUEUES[2], "amqp://x", lambda _m: None, stop)

    def missing():
        raise QueueUnavailable("ticket-service.RefundCompleted", 404, "NOT_FOUND")

    monkeypatch.setattr(consumer, "_consume", missing)

    consumer.run()  # returns instead of raising

    assert stop.waits == [QUEUE_MISSING_RETRY_SECONDS]


def test_amqp_correlation_id_is_used_when_the_envelope_has_none():
    from app.correlation import get_correlation_id  # pylint: disable=import-outside-toplevel

    seen = []

    dispose(
        envelope("OrderConfirmed", order_payload()),
        lambda _m: seen.append(get_correlation_id()),
        amqp_correlation_id="trace-from-checkout",
    )

    assert seen == ["trace-from-checkout"]


def test_without_an_amqp_correlation_id_none_is_bound():
    from app.correlation import get_correlation_id  # pylint: disable=import-outside-toplevel

    seen = []

    dispose(
        envelope("OrderConfirmed", order_payload()), lambda _m: seen.append(get_correlation_id())
    )

    assert seen == ["-"]  # so issuance can fall back to the envelope, then message_id
