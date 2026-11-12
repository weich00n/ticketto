from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.messaging.schemas import (
    EventCancelled,
    EventPublished,
    EventSoldOut,
    EventUpdated,
    OrderConfirmed,
    PoisonMessage,
    parse_message,
)
from tests.fakes import messages

pytestmark = pytest.mark.unit

EVENT = "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90"
RELEASE = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def test_every_consumed_message_parses() -> None:
    cases: list[tuple[messages.OutgoingMessage, type]] = [
        (messages.event_published(EVENT), EventPublished),
        (messages.event_updated(EVENT, version=2), EventUpdated),
        (messages.event_cancelled(EVENT, version=3), EventCancelled),
        (messages.event_sold_out(EVENT), EventSoldOut),
        (
            messages.order_confirmed(
                admission_id=str(uuid.uuid4()), user_id=str(uuid.uuid4()), event_id=EVENT
            ),
            OrderConfirmed,
        ),
    ]
    for outgoing, payload_type in cases:
        parsed = parse_message(outgoing.body)
        assert type(parsed.payload) is payload_type
        assert parsed.message_id == outgoing.message_id
        assert parsed.message_type == outgoing.message_type


def test_event_published_fields() -> None:
    msg = messages.event_published(
        EVENT,
        version=1,
        total_capacity=304,
        release_at=RELEASE,
        queue_join_opens_at=RELEASE - timedelta(hours=2),
        sale_ends_at=RELEASE + timedelta(days=7),
    )
    payload = parse_message(msg.body).payload
    assert isinstance(payload, EventPublished)
    assert (payload.event_id, payload.event_version, payload.total_capacity) == (EVENT, 1, 304)
    assert payload.release_at == RELEASE and payload.release_at.tzinfo is UTC
    assert payload.queue_join_opens_at == RELEASE - timedelta(hours=2)
    assert payload.sale_ends_at == RELEASE + timedelta(days=7)


def test_sale_ends_at_is_optional() -> None:
    payload = parse_message(messages.event_published(EVENT).body).payload
    assert isinstance(payload, EventPublished) and payload.sale_ends_at is None
    explicit_null = messages.event_published(EVENT).with_payload(starts_at=None)
    assert parse_message(explicit_null.body).payload.sale_ends_at is None  # type: ignore[union-attr]


def test_join_may_open_at_release() -> None:
    msg = messages.event_published(EVENT, release_at=RELEASE, queue_join_opens_at=RELEASE)
    assert parse_message(msg.body).payload.queue_join_opens_at == RELEASE  # type: ignore[union-attr]


def test_old_sale_ends_at_key_is_ignored() -> None:
    msg = messages.event_published(EVENT).with_payload(sale_ends_at="2026-11-27T12:00:00Z")
    assert parse_message(msg.body).payload.sale_ends_at is None  # type: ignore[union-attr]


def test_ids_are_canonicalised_and_offsets_converted_to_utc() -> None:
    msg = (
        messages.event_published(EVENT)
        .with_payload(event_id=EVENT.upper(), release_at="2026-11-20T20:00:00+08:00")
        .with_envelope(message_id=uuid.UUID(int=7).hex)
    )
    parsed = parse_message(msg.body)
    assert parsed.payload.event_id == EVENT
    assert parsed.message_id == str(uuid.UUID(int=7))
    assert parsed.payload.release_at == RELEASE  # type: ignore[union-attr]


def test_unknown_fields_are_ignored() -> None:
    msg = messages.event_sold_out(EVENT).with_payload(remaining=0).with_envelope(trace_id="t-1")
    assert isinstance(parse_message(msg.body).payload, EventSoldOut)


def _poison(body: bytes) -> str:
    with pytest.raises(PoisonMessage) as exc:
        parse_message(body)
    return str(exc.value)


def _mutated(**envelope: Any) -> bytes:
    return messages.event_published(EVENT).with_envelope(**envelope).body


def _payload(**payload: Any) -> bytes:
    return messages.event_published(EVENT).with_payload(**payload).body


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"not json", "not JSON"),
        (b"\xff\xfe", "not JSON"),
        (b"[1, 2]", "not a JSON object"),
        (json.dumps({"message_type": "EventPublished"}).encode(), "invalid envelope"),
        (_mutated(message_id="not-a-uuid"), "invalid envelope"),
        (_mutated(schema_version=2), "unsupported schema_version 2"),
        (_mutated(message_type="EventRenamed"), "unknown message_type"),
        (_mutated(occurred_at="2026-10-08T07:00:00"), "invalid envelope"),  # naive
        (_payload(event_version=0), "invalid EventPublished payload"),
        (_payload(total_capacity=-1), "invalid EventPublished payload"),
        (_payload(release_at="2026-11-20T12:00:00"), "invalid EventPublished payload"),
        (_payload(starts_at="2026-11-20T12:00:00Z"), "starts_at must be later"),
        (_payload(starts_at="2026-11-19T12:00:00Z"), "starts_at must be later"),
        (_payload(event_id=None), "invalid EventPublished payload"),
        (_payload(queue_join_opens_at=None), "invalid EventPublished payload"),
        (
            _payload(queue_join_opens_at="2026-11-20T12:00:00.001Z"),
            "queue_join_opens_at must not be later",
        ),
    ],
)
def test_poison_messages(body: bytes, reason: str) -> None:
    assert reason in _poison(body)


def test_order_confirmed_requires_every_id() -> None:
    msg = messages.order_confirmed(
        admission_id=str(uuid.uuid4()), user_id=str(uuid.uuid4()), event_id=EVENT
    )
    payload = dict(msg.envelope["payload"])
    del payload["admission_id"]
    assert "admission_id" in _poison(msg.with_envelope(payload=payload).body)
