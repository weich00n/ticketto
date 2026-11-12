import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from app import models  # noqa: F401  # pylint: disable=unused-import
from app.db import Base

pytestmark = pytest.mark.integration

MY_EVENTS = "/api/v1/organizer/events"


def create_and_publish(client, organiser, event_payload):
    created = client.post(MY_EVENTS, json=event_payload, headers=organiser)
    assert created.status_code == 201, created.text
    event = created.json()
    published = client.post(f"{MY_EVENTS}/{event['event_id']}/publish", headers=organiser)
    assert published.status_code == 200, published.text
    return event


def receive(channel, queue, event_id, count, timeout=20):
    messages, deadline = [], time.monotonic() + timeout
    while len(messages) < count and time.monotonic() < deadline:
        method, properties, body = channel.basic_get(queue, auto_ack=True)
        if method is None:
            time.sleep(0.5)
            continue
        message = json.loads(body)
        if message["payload"]["event_id"] == event_id:
            messages.append((method.routing_key, properties, message))
    return messages


def wait_for_status(client, organiser, event_id, status, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = client.get(f"{MY_EVENTS}/{event_id}", headers=organiser).json()["status"]
        if current == status:
            return True
        time.sleep(0.5)
    return False


def test_migrations_match_the_models(engine):
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        assert not compare_metadata(context, Base.metadata)


def test_concurrent_publishes_succeed_once(client, engine, organiser, event_payload):
    event = client.post(MY_EVENTS, json=event_payload, headers=organiser).json()
    url = f"{MY_EVENTS}/{event['event_id']}/publish"

    def attempt(_):
        return client.post(url, headers=organiser).status_code

    with ThreadPoolExecutor(5) as pool:
        codes = sorted(pool.map(attempt, range(5)))
    assert codes == [200, 409, 409, 409, 409]

    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT COUNT(*) FROM outbox_messages WHERE aggregate_id = :id"),
            {"id": event["event_id"]},
        ).scalar()
    assert rows == 1


def test_messages_are_delivered_in_order(client, organiser, event_payload, message_queue):
    channel, queue = message_queue
    event = create_and_publish(client, organiser, event_payload)
    cancelled = client.post(
        f"{MY_EVENTS}/{event['event_id']}/cancel",
        json={"reason": "Integration test"},
        headers={**organiser, "X-Correlation-Id": "it-cancel"},
    )
    assert cancelled.status_code == 200

    messages = receive(channel, queue, event["event_id"], count=2)
    assert [routing_key for routing_key, _, _ in messages] == ["event.published", "event.cancelled"]

    _, published_props, published = messages[0]
    assert published_props.message_id == published["message_id"]
    assert published_props.type == published["message_type"] == "EventPublished"
    # Publishing rules in rabbitmq-service docs/MESSAGING.md
    assert published_props.content_type == "application/json"
    assert published_props.delivery_mode == 2
    assert published["payload"]["ticket_categories"][0]["price_amount"] == 28800

    _, cancelled_props, cancelled_message = messages[1]
    assert cancelled_props.correlation_id == "it-cancel"
    assert cancelled_message["payload"]["cancelled_at"] == cancelled.json()["cancelled_at"]


def test_events_complete_once_they_start(client, engine, organiser, event_payload):
    event = create_and_publish(client, organiser, event_payload)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE events SET starts_at = :past WHERE event_id = :id"),
            {"past": datetime.now(UTC) - timedelta(minutes=1), "id": event["event_id"]},
        )
    assert wait_for_status(client, organiser, event["event_id"], "COMPLETED")


def test_accent_only_duplicate_names_are_a_400(client, organiser, event_payload):
    event_payload["ticket_categories"] = [
        {"name": "Café", "price_amount": 1000, "total_quantity": 10},
        {"name": "Cafe", "price_amount": 1000, "total_quantity": 10},
    ]
    response = client.post(MY_EVENTS, json=event_payload, headers=organiser)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_timestamps_are_stored_in_utc(client, organiser, event_payload):
    event = client.post(MY_EVENTS, json=event_payload, headers=organiser).json()
    created_at = datetime.fromisoformat(event["created_at"])
    assert abs(created_at - datetime.now(UTC)) < timedelta(minutes=2)


def test_internal_category_lookup(client, organiser, event_payload):
    event = create_and_publish(client, organiser, event_payload)
    category = event["ticket_categories"][0]
    response = client.get(
        f"/internal/v1/events/{event['event_id']}/categories/{category['category_id']}"
    )
    assert response.status_code == 200
    assert response.json()["price_amount"] == 28800
