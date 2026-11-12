"""End to end against the running API and worker, MySQL and RabbitMQ."""

import os
import threading
import time
import uuid

import pika
import pika.exceptions
import pytest

from tests.integration.conftest import (
    STAFF,
    order_payload,
    request,
    tickets_for,
    wait_until,
)

pytestmark = pytest.mark.integration


def _confirm(publisher, payload, message_id=None):
    return publisher.publish(
        "checkout.events", "order.confirmed", "OrderConfirmed", payload, message_id
    )


def _issued(payload, count):
    return wait_until(
        lambda: (
            (t := tickets_for(payload["order_id"], payload["user_id"])) and len(t) == count and t
        ),
        what=f"{count} tickets for {payload['order_id']}",
    )


def test_health():
    assert request("GET", "/health") == (200, {"status": "ok", "service": "ticket-service"})
    assert request("GET", "/ready") == (200, {"status": "ready"})


def test_confirmed_order_gets_tickets_and_publishes_ticket_issued(publisher, ticket_issued):
    payload = order_payload(quantity=2)

    _confirm(publisher, payload)

    tickets = _issued(payload, 2)
    assert {t["status"] for t in tickets} == {"VALID"}
    properties, body = ticket_issued.find(payload["order_id"])
    # As rabbitmq-service's MESSAGING.md asks of every publisher.
    assert properties.delivery_mode == 2 and properties.content_type == "application/json"
    assert (properties.type, properties.message_id) == ("TicketIssued", body["message_id"])
    assert properties.correlation_id
    assert (body["message_type"], body["producer"], body["schema_version"]) == (
        "TicketIssued",
        "ticket-service",
        1,
    )
    message = body["payload"]
    assert (message["user_id"], message["event_id"]) == (payload["user_id"], payload["event_id"])
    assert message["delivery_email"] == payload["delivery_email"]
    assert {t["ticket_id"]: t["qr_payload"] for t in message["tickets"]} == {
        t["ticket_id"]: t["qr_payload"] for t in tickets
    }
    order = wait_until(
        lambda: (
            (r := request("GET", f"/internal/v1/orders/{payload['order_id']}/tickets")[1])
            and r["ticket_issued"]["status"] == "PUBLISHED"
            and r
        ),
        what="TicketIssued marked published",
    )
    assert order["issuance_status"] == "ISSUED"
    assert order["ticket_issued"]["message_id"] == body["message_id"]


def test_republish_sends_ticket_issued_again_with_a_new_id(publisher, ticket_issued):
    payload = order_payload(quantity=1)
    _confirm(publisher, payload)
    _issued(payload, 1)
    _, first = ticket_issued.find(payload["order_id"])

    status, body = request(
        "POST", f"/internal/v1/orders/{payload['order_id']}/ticket-issued/republish"
    )

    assert status == 202, body
    _, again = wait_until(
        lambda: (
            (m := ticket_issued.find(payload["order_id"]))[1]["message_id"] != first["message_id"]
            and m
        ),
        what="republished TicketIssued",
    )
    assert again["message_id"] == body["message_id"]


def test_duplicate_messages_do_not_duplicate_tickets(publisher):
    payload = order_payload(quantity=3)
    message_id = _confirm(publisher, payload)
    _issued(payload, 3)

    _confirm(publisher, payload, message_id)  # redelivery of the same message
    _confirm(publisher, payload)  # same order, new message ID
    marker = order_payload(quantity=1)  # processed after the duplicates on the same queue
    _confirm(publisher, marker)
    _issued(marker, 1)

    assert len(tickets_for(payload["order_id"])) == 3


def test_concurrent_scans_admit_exactly_once(publisher):
    payload = order_payload(quantity=1)
    _confirm(publisher, payload)
    ticket = _issued(payload, 1)[0]
    results: list[int] = []
    barrier = threading.Barrier(10)

    def scan():
        barrier.wait()
        status, _ = request(
            "POST",
            "/api/v1/tickets/check-in",
            headers=STAFF,
            body={"qr_payload": ticket["qr_payload"], "event_id": payload["event_id"]},
        )
        results.append(status)

    threads = [threading.Thread(target=scan) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [200] + [409] * 9


def test_cancellation_invalidates_tickets_and_rejects_late_confirmations(publisher):
    payload = order_payload(quantity=2)
    _confirm(publisher, payload)
    _issued(payload, 2)

    publisher.publish(
        "event.events",
        "event.cancelled",
        "EventCancelled",
        {"event_id": payload["event_id"], "event_version": 3, "reason": "Venue closure"},
    )

    wait_until(
        lambda: {t["status"] for t in tickets_for(payload["order_id"])} == {"INVALIDATED"},
        what="tickets invalidated",
    )
    late = order_payload(event_id=payload["event_id"], quantity=1)
    _confirm(publisher, late)
    order = wait_until(
        lambda: (
            (r := request("GET", f"/internal/v1/orders/{late['order_id']}/tickets"))[0] == 200
            and r[1]
        ),
        what="late order recorded",
    )
    assert (order["issuance_status"], order["rejection_reason"]) == ("REJECTED", "EVENT_CANCELLED")
    assert order["tickets"] == []


def test_refund_invalidates_the_order(publisher):
    payload = order_payload(quantity=1)
    _confirm(publisher, payload)
    _issued(payload, 1)

    publisher.publish(
        "checkout.events",
        "refund.completed",
        "RefundCompleted",
        {
            "order_id": payload["order_id"],
            "event_id": payload["event_id"],
            "refund_id": "re_it",
            "refund_amount": 28800,
            "currency": "SGD",
        },
    )

    wait_until(
        lambda: tickets_for(payload["order_id"])[0]["invalidation_reason"] == "ORDER_REFUNDED",
        what="refunded ticket invalidated",
    )


def test_poison_message_is_dead_lettered(publisher):
    dlq = "ticket-service.OrderConfirmed.dlq"
    before = publisher.message_count(dlq)

    publisher.raw("checkout.events", "order.confirmed", b'{"not": "an envelope"}')

    wait_until(lambda: publisher.message_count(dlq) == before + 1, what="message in DLQ")


def test_refund_before_confirmation_rejects_the_late_order(publisher):
    payload = order_payload(quantity=2)
    publisher.publish(
        "checkout.events",
        "refund.completed",
        "RefundCompleted",
        {"order_id": payload["order_id"], "event_id": payload["event_id"]},
    )
    marker = order_payload(quantity=1, event_id=payload["event_id"])
    _confirm(publisher, marker)
    _issued(marker, 1)  # the refund queue is separate, so also wait for it to drain
    wait_until(
        lambda: publisher.message_count("ticket-service.RefundCompleted") == 0,
        what="refund delivered",
    )
    time.sleep(1)  # delivered is not yet committed; handling takes milliseconds

    _confirm(publisher, payload)

    order = wait_until(
        lambda: (
            (r := request("GET", f"/internal/v1/orders/{payload['order_id']}/tickets"))[0] == 200
            and r[1]
        ),
        what="late order recorded",
    )
    assert (order["issuance_status"], order["rejection_reason"]) == ("REJECTED", "ORDER_REFUNDED")


def test_event_details_from_event_published_appear_on_tickets(publisher):
    payload = order_payload(quantity=1)
    publisher.publish(
        "event.events",
        "event.published",
        "EventPublished",
        {
            "event_id": payload["event_id"],
            "event_version": 1,
            "title": "Integration Night",
            "venue": "Test Hall",
            "starts_at": "2026-12-01T12:00:00Z",
            "ticket_categories": [{"category_id": payload["category_id"], "name": "CAT 2"}],
        },
    )
    wait_until(
        lambda: publisher.message_count("ticket-service.EventPublished") == 0,
        what="EventPublished delivered",
    )
    time.sleep(1)  # delivered is not yet committed

    _confirm(publisher, payload)

    ticket = _issued(payload, 1)[0]
    assert ticket["event"]["title"] == "Integration Night"
    assert ticket["event"]["venue"] == "Test Hall"
    assert ticket["category_name"] == "CAT 2"


@pytest.mark.skipif(
    not os.environ.get("IT_TICKET_RABBITMQ_URL"), reason="Ticket's own broker user not given"
)
def test_tickets_broker_user_has_only_the_team_brokers_permissions():
    # The worker runs as this user. If it could declare queues, CI would not catch code that
    # the real broker (rabbitmq-service) refuses.
    connection = pika.BlockingConnection(pika.URLParameters(os.environ["IT_TICKET_RABBITMQ_URL"]))
    try:
        channel = connection.channel()
        channel.queue_declare("ticket-service.OrderConfirmed", passive=True)  # may read its own
        with pytest.raises(pika.exceptions.ChannelClosedByBroker) as refused:
            channel.queue_declare(f"ticket-service.Probe{uuid.uuid4().hex[:8]}", durable=True)
        assert refused.value.reply_code == 403
    finally:
        if connection.is_open:
            connection.close()


@pytest.mark.skipif(
    not os.environ.get("IT_TICKET_RABBITMQ_URL"), reason="Ticket's own broker user not given"
)
def test_tickets_broker_user_may_publish_only_ticket_issued():
    connection = pika.BlockingConnection(pika.URLParameters(os.environ["IT_TICKET_RABBITMQ_URL"]))
    try:
        channel = connection.channel()
        channel.exchange_declare("ticket.events", exchange_type="topic", durable=True)  # allowed
        channel.confirm_delivery()
        channel.basic_publish("ticket.events", "ticket.issued", b"{}", mandatory=True)  # allowed
        with pytest.raises(pika.exceptions.ChannelClosedByBroker) as refused:
            channel.basic_publish("ticket.events", "ticket.refunded", b"{}")
        assert refused.value.reply_code == 403
    finally:
        if connection.is_open:
            connection.close()
