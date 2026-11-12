"""Tests against the running API and worker, with real MySQL and RabbitMQ.

Run with `docker compose run --rm tests`. Each test uses its own organiser.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pika
import pytest
from sqlalchemy import create_engine

SERVICE_URL = os.environ.get("IT_SERVICE_URL", "http://127.0.0.1:3001")
DATABASE_URL = os.environ.get(
    "IT_DATABASE_URL", "mysql+pymysql://ticketto:ticketto@127.0.0.1:3307/event_service"
)
RABBITMQ_URL = os.environ.get("IT_RABBITMQ_URL", "amqp://guest:guest@127.0.0.1:5672/%2F")


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=SERVICE_URL, timeout=10) as http:
        try:
            http.get("/ready").raise_for_status()
        except httpx.HTTPError as error:
            pytest.fail(
                f"Event Service is not reachable at {SERVICE_URL} ({error}). "
                "Start the stack first: docker compose up -d --build"
            )
        yield http


@pytest.fixture(scope="session")
def engine():
    db = create_engine(DATABASE_URL)
    yield db
    db.dispose()


@pytest.fixture()
def organiser():
    return {
        "X-User-Sub": str(uuid.uuid4()),
        "X-User-Username": "it-organiser@example.com",
        "X-User-Groups": "[organiser]",
    }


@pytest.fixture()
def event_payload():
    release = datetime.now(UTC).replace(microsecond=0) + timedelta(days=7)
    return {
        "title": f"Integration {uuid.uuid4().hex[:8]}",
        "description": "Created by the integration tests.",
        "venue": "Test Hall",
        "starts_at": iso(release + timedelta(days=1)),
        "ticket_release_at": iso(release),
        "ticket_categories": [{"name": "CAT 1", "price_amount": 28800, "total_quantity": 100}],
    }


@pytest.fixture()
def message_queue():
    """A private queue bound to every Event Service routing key, deleted after the test."""
    connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
    channel = connection.channel()
    channel.exchange_declare(exchange="event.events", exchange_type="topic", durable=True)
    queue = channel.queue_declare(queue="", exclusive=True).method.queue
    channel.queue_bind(queue=queue, exchange="event.events", routing_key="event.*")
    yield channel, queue
    connection.close()
