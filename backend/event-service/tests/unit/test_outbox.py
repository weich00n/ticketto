import pytest
from sqlalchemy import select

from app import outbox
from app.models import OutboxMessage

MY_EVENTS = "/api/v1/organizer/events"


class FakeChannel:
    def __init__(self, fail_first: bool):
        self.fail_first = fail_first
        self.sent: list[str] = []

    def exchange_declare(self, **_kwargs):
        pass

    def confirm_delivery(self):
        pass

    def basic_publish(self, *, routing_key, **_kwargs):
        if self.fail_first and not self.sent:
            self.fail_first = False
            raise RuntimeError("broker unavailable")
        self.sent.append(routing_key)


class FakeConnection:
    def __init__(self, channel):
        self._channel = channel

    def channel(self):
        return self._channel

    def close(self):
        pass


@pytest.fixture()
def two_messages(client, organiser_headers, event_payload):
    event = client.post(MY_EVENTS, json=event_payload, headers=organiser_headers).get_json()
    url = f"{MY_EVENTS}/{event['event_id']}"
    client.post(f"{url}/publish", headers=organiser_headers)
    client.post(f"{url}/cancel", json={"reason": "Storm"}, headers=organiser_headers)


def _send(monkeypatch, session_factory, channel):
    monkeypatch.setattr(outbox.pika, "BlockingConnection", lambda _params: FakeConnection(channel))
    return outbox.publish_pending_messages(session_factory, "amqp://unused")


def _undelivered(session_factory):
    with session_factory() as session:
        rows = session.scalars(
            select(OutboxMessage).where(OutboxMessage.delivered_at.is_(None))
        ).all()
        return [(row.routing_key, row.attempts) for row in rows]


@pytest.mark.usefixtures("two_messages")
def test_messages_are_sent_in_order(monkeypatch, session_factory):
    channel = FakeChannel(fail_first=False)
    assert _send(monkeypatch, session_factory, channel) == 2
    assert channel.sent == ["event.published", "event.cancelled"]
    assert _undelivered(session_factory) == []


@pytest.mark.usefixtures("two_messages")
def test_a_failure_stops_the_batch_so_order_is_kept(monkeypatch, session_factory):
    channel = FakeChannel(fail_first=True)
    assert _send(monkeypatch, session_factory, channel) == 0
    assert not channel.sent
    assert _undelivered(session_factory) == [("event.published", 1), ("event.cancelled", 0)]

    assert _send(monkeypatch, session_factory, channel) == 2
    assert channel.sent == ["event.published", "event.cancelled"]


class PoisonChannel(FakeChannel):
    """Refuses one routing key every time, as an unroutable message would be."""

    def __init__(self, bad_key: str):
        super().__init__(fail_first=False)
        self.bad_key = bad_key

    def basic_publish(self, *, routing_key, **kwargs):
        if routing_key == self.bad_key:
            raise RuntimeError("unroutable")
        super().basic_publish(routing_key=routing_key, **kwargs)


@pytest.mark.usefixtures("two_messages")
def test_a_message_that_keeps_failing_is_given_up_so_the_next_goes_out(
    monkeypatch, session_factory, caplog
):
    channel = PoisonChannel("event.published")
    for _ in range(outbox.MAX_DELIVERY_ATTEMPTS):
        _send(monkeypatch, session_factory, channel)
    assert channel.sent == ["event.cancelled"]
    assert _undelivered(session_factory) == [("event.published", outbox.MAX_DELIVERY_ATTEMPTS)]
    assert any(
        record.levelname == "ERROR" and "given up" in record.getMessage()
        for record in caplog.records
    )

    # It is no longer retried, and it does not block later messages.
    assert _send(monkeypatch, session_factory, channel) == 0
    assert _undelivered(session_factory) == [("event.published", outbox.MAX_DELIVERY_ATTEMPTS)]


@pytest.mark.usefixtures("two_messages")
def test_a_broker_outage_does_not_use_up_a_messages_attempts(monkeypatch, session_factory):
    class DownChannel(FakeChannel):
        def basic_publish(self, **_kwargs):
            raise outbox.pika.exceptions.StreamLostError("broker went away")

    for _ in range(outbox.MAX_DELIVERY_ATTEMPTS + 2):
        _send(monkeypatch, session_factory, DownChannel(fail_first=False))
    assert _undelivered(session_factory) == [("event.published", 0), ("event.cancelled", 0)]
