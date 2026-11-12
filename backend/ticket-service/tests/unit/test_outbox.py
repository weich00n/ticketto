import json
from datetime import timedelta

import pika
import pytest
from sqlalchemy import select, update

from app import outbox
from app.issuance import utcnow
from app.models import CHECKED_IN, EventState, OutboxMessage, Ticket
from app.outbox import RabbitPublisher, retry_delay, run_once
from tests.unit.conftest import event_published, order_payload

# The payload agreed with notification-service, and the team broker's envelope.
PAYLOAD_FIELDS = {
    "order_id",
    "user_id",
    "event_id",
    "delivery_email",
    "title",
    "venue",
    "starts_at",
    "category_name",
    "tickets",
}
ENVELOPE_FIELDS = {
    "message_id",
    "message_type",
    "schema_version",
    "occurred_at",
    "producer",
    "payload",
}


class FakePublisher:
    """Records what would reach the broker. Fails the first `fail_times` publishes."""

    def __init__(self, fail_times: int = 0, before_publish=None):
        self.published: list[dict] = []
        self.attempted: list[str] = []
        self.fail_times = fail_times
        self.before_publish = before_publish

    def publish(self, routing_key, body, *, message_id, message_type, correlation_id):
        self.attempted.append(message_id)
        if self.before_publish:
            self.before_publish()
        if self.fail_times:
            self.fail_times -= 1
            raise ConnectionError("broker unavailable")
        self.published.append(
            {
                "routing_key": routing_key,
                "message_id": message_id,
                "message_type": message_type,
                "correlation_id": correlation_id,
                "body": json.loads(body),
            }
        )


def outbox_row(session_factory, order_id) -> OutboxMessage:
    """The order's latest TicketIssued row."""
    with session_factory() as session:
        return session.scalars(
            select(OutboxMessage)
            .where(OutboxMessage.order_id == order_id)
            .order_by(OutboxMessage.id.desc())
        ).first()


def make_due(session_factory, order_id) -> None:
    with session_factory() as session, session.begin():
        session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.order_id == order_id)
            .values(next_attempt_at=utcnow() - timedelta(seconds=1))
        )


def _tickets_api(client, payload):
    return client.get(
        f"/api/v1/tickets?order_id={payload['order_id']}",
        headers={"X-User-Id": payload["user_id"]},
    ).get_json()["items"]


def test_publishes_one_ticket_issued_per_order_with_the_agreed_payload(
    client, deliver, session_factory, settings
):
    payload = order_payload(quantity=3)
    deliver(
        "EventPublished",
        event_published(payload["event_id"], payload["category_id"], venue="Esplanade Hall"),
    )
    deliver("OrderConfirmed", payload)
    publisher = FakePublisher()

    assert run_once(session_factory, settings, publisher) == 1

    assert len(publisher.published) == 1
    sent = publisher.published[0]
    row = outbox_row(session_factory, payload["order_id"])
    assert (sent["routing_key"], sent["message_type"]) == ("ticket.issued", "TicketIssued")
    assert sent["message_id"] == row.message_id
    body = sent["body"]
    assert set(body) == ENVELOPE_FIELDS
    assert (body["message_id"], body["message_type"]) == (row.message_id, "TicketIssued")
    assert (body["schema_version"], body["producer"]) == (1, "ticket-service")
    assert body["occurred_at"].endswith("Z")

    message = body["payload"]
    assert set(message) == PAYLOAD_FIELDS
    assert message["order_id"] == payload["order_id"]
    assert message["user_id"] == payload["user_id"]
    assert message["event_id"] == payload["event_id"]
    assert message["delivery_email"] == "buyer@example.com"
    assert (message["title"], message["venue"]) == ("The Midnight Symphony", "Esplanade Hall")
    assert message["starts_at"].endswith("Z")
    assert message["category_name"] == "CAT 1"
    # One entry per ticket, with the same QR payload the buyer sees in the app.
    from_api = {t["ticket_id"]: t["qr_payload"] for t in _tickets_api(client, payload)}
    assert [t["ticket_number"] for t in message["tickets"]] == [1, 2, 3]
    assert all(set(t) == {"ticket_id", "ticket_number", "qr_payload"} for t in message["tickets"])
    assert {t["ticket_id"]: t["qr_payload"] for t in message["tickets"]} == from_api

    assert (row.status, row.attempts, row.last_error) == ("PUBLISHED", 1, None)
    assert row.published_at is not None and row.next_attempt_at is None
    assert run_once(session_factory, settings, publisher) == 0  # nothing left to publish


def test_event_details_ticket_does_not_know_are_null(deliver, session_factory, settings):
    payload = order_payload(quantity=1)
    deliver("OrderConfirmed", payload)  # no EventPublished, no Event Service
    publisher = FakePublisher()

    run_once(session_factory, settings, publisher)

    message = publisher.published[0]["body"]["payload"]
    assert [message[k] for k in ("title", "venue", "starts_at", "category_name")] == [None] * 4
    assert len(message["tickets"]) == 1


def test_missing_delivery_email_is_published_as_null(deliver, session_factory, settings):
    payload = order_payload()
    del payload["delivery_email"]
    deliver("OrderConfirmed", payload)
    publisher = FakePublisher()

    run_once(session_factory, settings, publisher)

    assert publisher.published[0]["body"]["payload"]["delivery_email"] is None


def test_correlation_id_travels_as_the_amqp_property_only(deliver, session_factory, settings):
    payload = order_payload()
    deliver("OrderConfirmed", payload)
    publisher = FakePublisher()

    run_once(session_factory, settings, publisher)

    sent = publisher.published[0]
    row = outbox_row(session_factory, payload["order_id"])
    assert row.correlation_id is not None
    assert sent["correlation_id"] == row.correlation_id
    assert "correlation_id" not in sent["body"]  # the envelope schema allows no extra fields


def test_failure_is_retried_with_backoff_and_the_same_message_id(
    deliver, session_factory, settings
):
    payload = order_payload()
    deliver("OrderConfirmed", payload)
    publisher = FakePublisher(fail_times=1)

    run_once(session_factory, settings, publisher)

    row = outbox_row(session_factory, payload["order_id"])
    assert (row.status, row.attempts) == ("PENDING", 1)
    assert "ConnectionError: broker unavailable" in row.last_error
    wait = row.next_attempt_at.replace(tzinfo=None) - utcnow().replace(tzinfo=None)
    assert timedelta(seconds=3) < wait <= timedelta(seconds=5)
    assert run_once(session_factory, settings, publisher) == 0  # not due yet

    make_due(session_factory, payload["order_id"])
    run_once(session_factory, settings, publisher)

    row = outbox_row(session_factory, payload["order_id"])
    assert (row.status, row.attempts, row.last_error) == ("PUBLISHED", 2, None)
    assert publisher.attempted == [row.message_id, row.message_id]


def test_backoff_doubles_up_to_its_cap_and_never_gives_up(settings):
    delays = [retry_delay(settings, n).total_seconds() for n in (1, 2, 3, 7, 8, 500)]
    assert delays == [5, 10, 20, 300, 300, 300]


def test_messages_stay_pending_however_often_the_broker_refuses(deliver, session_factory, settings):
    payload = order_payload()
    deliver("OrderConfirmed", payload)
    publisher = FakePublisher(fail_times=99)

    for _ in range(12):
        make_due(session_factory, payload["order_id"])
        run_once(session_factory, settings, publisher)

    row = outbox_row(session_factory, payload["order_id"])
    assert (row.status, row.attempts) == ("PENDING", 12)


def test_invalidated_order_is_not_published(deliver, session_factory, settings):
    payload = order_payload()
    deliver("OrderConfirmed", payload)
    deliver("RefundCompleted", {"order_id": payload["order_id"], "event_id": payload["event_id"]})
    publisher = FakePublisher()

    assert run_once(session_factory, settings, publisher) == 0  # already cancelled

    assert not publisher.published
    assert outbox_row(session_factory, payload["order_id"]).status == "CANCELLED"


def test_order_invalidated_after_its_claim_is_cancelled_not_published(
    deliver, session_factory, settings, monkeypatch
):
    payload = order_payload()
    deliver("OrderConfirmed", payload)
    claim_due = outbox.claim_due

    def claim_then_cancel(*args, **kwargs):
        claims = claim_due(*args, **kwargs)
        deliver("EventCancelled", {"event_id": payload["event_id"]})
        return claims

    monkeypatch.setattr(outbox, "claim_due", claim_then_cancel)
    publisher = FakePublisher()

    run_once(session_factory, settings, publisher)

    assert not publisher.published
    assert outbox_row(session_factory, payload["order_id"]).status == "CANCELLED"


def test_cancellation_while_publishing_still_records_the_publish(
    deliver, session_factory, settings
):
    payload = order_payload()
    deliver("OrderConfirmed", payload)
    publisher = FakePublisher(
        before_publish=lambda: deliver("EventCancelled", {"event_id": payload["event_id"]})
    )

    run_once(session_factory, settings, publisher)

    # The broker has it, so the record must say so.
    assert outbox_row(session_factory, payload["order_id"]).status == "PUBLISHED"


def test_failed_publish_after_a_cancellation_stays_cancelled(deliver, session_factory, settings):
    payload = order_payload()
    deliver("OrderConfirmed", payload)
    publisher = FakePublisher(
        fail_times=1,
        before_publish=lambda: deliver("EventCancelled", {"event_id": payload["event_id"]}),
    )

    run_once(session_factory, settings, publisher)

    row = outbox_row(session_factory, payload["order_id"])
    assert row.status == "CANCELLED" and row.next_attempt_at is None


def test_only_tickets_still_valid_are_sent(deliver, session_factory, settings):
    payload = order_payload(quantity=2)
    deliver("OrderConfirmed", payload)
    with session_factory() as session, session.begin():
        session.execute(
            update(Ticket)
            .where(Ticket.order_id == payload["order_id"], Ticket.ticket_number == 1)
            .values(status=CHECKED_IN)
        )
    publisher = FakePublisher()

    run_once(session_factory, settings, publisher)

    tickets = publisher.published[0]["body"]["payload"]["tickets"]
    assert [t["ticket_number"] for t in tickets] == [2]


def test_missing_event_details_are_fetched_from_event_service(
    deliver, session_factory, settings, monkeypatch
):
    payload = order_payload(quantity=1)
    deliver("OrderConfirmed", payload)
    calls = []

    def fake_fetch(base_url, event_id):
        calls.append((base_url, event_id))
        return {
            "event_id": event_id,
            "title": "Autumn Live",
            "venue": "Example Hall",
            "starts_at": "2026-11-20T12:00:00Z",
            "ticket_categories": [{"category_id": payload["category_id"], "name": "CAT 2"}],
        }

    monkeypatch.setattr("app.event_details.fetch_event", fake_fetch)
    publisher = FakePublisher()
    with_event_service = type(settings)(
        **{**settings.__dict__, "event_service_url": "http://event:3001"}
    )

    run_once(session_factory, with_event_service, publisher)

    assert calls == [("http://event:3001", payload["event_id"])]
    message = publisher.published[0]["body"]["payload"]
    assert (message["title"], message["venue"], message["starts_at"]) == (
        "Autumn Live",
        "Example Hall",
        "2026-11-20T12:00:00Z",
    )
    assert message["category_name"] == "CAT 2"
    with session_factory() as session:
        assert session.get(EventState, payload["event_id"]).venue == "Example Hall"


def test_unreachable_event_service_does_not_block_the_message(deliver, session_factory, settings):
    payload = order_payload(quantity=1)
    deliver("OrderConfirmed", payload)
    publisher = FakePublisher()
    unreachable = type(settings)(**{**settings.__dict__, "event_service_url": "http://127.0.0.1:9"})

    run_once(session_factory, unreachable, publisher)

    assert publisher.published[0]["body"]["payload"]["title"] is None


def test_a_crash_while_delivering_leaves_the_row_for_its_lease(
    deliver, session_factory, settings, monkeypatch
):
    payload = order_payload()
    deliver("OrderConfirmed", payload)

    def crash(*_args, **_kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(outbox, "deliver", crash)

    assert run_once(session_factory, settings, FakePublisher()) == 1  # logged, not raised

    row = outbox_row(session_factory, payload["order_id"])
    lease = row.next_attempt_at.replace(tzinfo=None) - utcnow().replace(tzinfo=None)
    assert row.status == "PENDING" and timedelta(minutes=1) < lease <= outbox.CLAIM_LEASE


# ---------- RabbitPublisher ----------


class FakeChannel:
    def __init__(self, fail_publish=False):
        self.is_open = True
        self.calls: list[tuple] = []
        self.fail_publish = fail_publish

    def exchange_declare(self, **kwargs):
        self.calls.append(("exchange_declare", kwargs))

    def confirm_delivery(self):
        self.calls.append(("confirm_delivery",))

    def basic_publish(self, exchange, routing_key, body, properties=None, mandatory=False):
        sent = {
            "exchange": exchange,
            "routing_key": routing_key,
            "body": body,
            "properties": properties,
            "mandatory": mandatory,
        }
        self.calls.append(("basic_publish", sent))
        if self.fail_publish:
            raise pika.exceptions.UnroutableError([])


class FakeConnection:
    instances: list["FakeConnection"] = []

    def __init__(self, parameters, fail_publish=False, fail_events=False):
        self.parameters = parameters
        self.is_open = True
        self.channel_ = FakeChannel(fail_publish)
        self.fail_events = fail_events
        self.events = 0
        FakeConnection.instances.append(self)

    def channel(self):
        return self.channel_

    def process_data_events(self, time_limit):
        assert time_limit == 0
        self.events += 1
        if self.fail_events:
            raise pika.exceptions.StreamLostError("gone")

    def close(self):
        self.is_open = False


@pytest.fixture()
def fake_pika(monkeypatch):
    FakeConnection.instances = []
    options = {}

    def connect(parameters):
        return FakeConnection(parameters, **options)

    monkeypatch.setattr(outbox.pika, "BlockingConnection", connect)
    return options


def _publish(publisher):
    publisher.publish(
        "ticket.issued",
        b"{}",
        message_id="6f0d3c2a-8b1e-4f7a-9c5d-2e4b6a8c0d1f",
        message_type="TicketIssued",
        correlation_id="req-1",
    )


@pytest.mark.usefixtures("fake_pika")
def test_rabbit_publisher_follows_the_brokers_publishing_rules():
    publisher = RabbitPublisher("amqp://ticket-service:pw@broker:5672/%2F")

    _publish(publisher)
    _publish(publisher)

    assert len(FakeConnection.instances) == 1  # one connection, reused
    connection = FakeConnection.instances[0]
    assert connection.parameters.host == "broker"
    calls = connection.channel_.calls
    assert calls[0] == (
        "exchange_declare",
        {"exchange": "ticket.events", "exchange_type": "topic", "durable": True},
    )
    assert calls[1] == ("confirm_delivery",)
    name, sent = calls[2]
    assert name == "basic_publish"
    assert (sent["exchange"], sent["routing_key"], sent["mandatory"]) == (
        "ticket.events",
        "ticket.issued",
        True,
    )
    properties = sent["properties"]
    assert properties.delivery_mode == pika.DeliveryMode.Persistent.value
    assert properties.content_type == "application/json"
    assert (properties.message_id, properties.type, properties.correlation_id) == (
        "6f0d3c2a-8b1e-4f7a-9c5d-2e4b6a8c0d1f",
        "TicketIssued",
        "req-1",
    )


def test_rabbit_publisher_reconnects_after_a_refused_publish(fake_pika):
    fake_pika["fail_publish"] = True
    publisher = RabbitPublisher("amqp://ticket-service:pw@broker:5672/%2F")

    with pytest.raises(pika.exceptions.UnroutableError):
        _publish(publisher)
    fake_pika["fail_publish"] = False
    _publish(publisher)

    assert len(FakeConnection.instances) == 2
    assert not FakeConnection.instances[0].is_open and FakeConnection.instances[1].is_open


@pytest.mark.usefixtures("fake_pika")
def test_rabbit_publisher_keepalive_drops_a_dead_connection():
    publisher = RabbitPublisher("amqp://ticket-service:pw@broker:5672/%2F")
    publisher.keepalive()  # nothing open yet: nothing to do
    _publish(publisher)
    publisher.keepalive()
    assert FakeConnection.instances[0].events == 1

    FakeConnection.instances[0].fail_events = True
    publisher.keepalive()
    publisher.close()

    assert not FakeConnection.instances[0].is_open
    _publish(publisher)
    assert len(FakeConnection.instances) == 2
