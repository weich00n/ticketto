"""Pins the published message format. A change here is a change for every consumer."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import pytest
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.models import OutboxMessage

MY_EVENTS = "/api/v1/organizer/events"


def _canonical_uuid(value: str) -> str:
    assert str(uuid.UUID(value)) == value
    return value


def _whole_second_utc(value: datetime) -> datetime:
    assert value.utcoffset() == UTC.utcoffset(None)
    assert value.microsecond == 0
    return value


Uuid = Annotated[str, AfterValidator(_canonical_uuid)]
Timestamp = Annotated[AwareDatetime, AfterValidator(_whole_second_utc)]


class _Exact(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Envelope(_Exact):
    message_id: Uuid
    message_type: Literal["EventPublished", "EventCancelled"]
    schema_version: Literal[1]
    occurred_at: Timestamp
    producer: Literal["event-service"]
    payload: dict


class Category(_Exact):
    category_id: Uuid
    name: str
    price_amount: int = Field(ge=0)
    currency: Literal["SGD"]
    total_quantity: int = Field(ge=1)


class EventPublished(_Exact):
    event_id: Uuid
    event_version: int = Field(ge=1)
    title: str
    venue: str
    starts_at: Timestamp
    release_at: Timestamp
    queue_join_opens_at: Timestamp
    published_at: Timestamp
    total_capacity: int = Field(ge=1)
    ticket_categories: list[Category]


class EventCancelled(_Exact):
    event_id: Uuid
    event_version: int = Field(ge=1)
    cancellation_id: Uuid
    cancelled_at: Timestamp
    reason: str


ROUTING_KEYS = {"EventPublished": "event.published", "EventCancelled": "event.cancelled"}


@pytest.fixture()
def messages(client, session_factory, organiser_headers, event_payload):
    event = client.post(MY_EVENTS, json=event_payload, headers=organiser_headers).get_json()
    url = f"{MY_EVENTS}/{event['event_id']}"
    assert client.post(f"{url}/publish", headers=organiser_headers).status_code == 200
    cancel = client.post(f"{url}/cancel", json={"reason": "Storm"}, headers=organiser_headers)
    assert cancel.status_code == 200
    with session_factory() as session:
        return session.scalars(select(OutboxMessage).order_by(OutboxMessage.occurred_at)).all()


def test_envelope_and_routing(messages):
    for message in messages:
        envelope = Envelope.model_validate(message.payload)
        assert envelope.message_id == message.message_id
        assert envelope.message_type == message.message_type
        assert message.routing_key == ROUTING_KEYS[message.message_type]


def test_payloads(messages, event_payload):
    published, cancelled = messages
    event_published = EventPublished.model_validate(published.payload["payload"])
    event_cancelled = EventCancelled.model_validate(cancelled.payload["payload"])
    assert event_published.venue == event_payload["venue"]
    # rabbitmq-service contracts/EventPublished.payload.schema.json
    assert event_published.queue_join_opens_at == event_published.release_at - timedelta(minutes=15)
    assert event_published.total_capacity == sum(
        c.total_quantity for c in event_published.ticket_categories
    )
    assert event_cancelled.event_id == event_published.event_id
    assert event_cancelled.event_version > event_published.event_version
