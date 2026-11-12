"""Real consumer threads: fakes publish via RabbitMQ, effects land in MySQL/Redis,
bad messages land in the DLQ."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from random import Random

import pytest
from pika.adapters.blocking_connection import BlockingChannel
from redis.asyncio import Redis

from app.core.redis import server_now
from app.core.status import EntryStatus, QueueStatus
from app.messaging import consumers
from app.messaging.consumers import Handler, heartbeat_key
from app.messaging.handlers import MessageHandlers
from app.messaging.schemas import Message
from app.messaging.topology import (
    EVENT_CANCELLED,
    EVENT_PUBLISHED,
    EVENT_SOLD_OUT,
    MAX_ATTEMPTS,
    ORDER_CONFIRMED,
)
from app.queue.config_repo import ConfigRepository
from app.queue.repository import QueueRepository
from tests.fakes import messages
from tests.fakes.checkout_service import FakeCheckoutService
from tests.fakes.event_service import FakeEventService
from tests.fakes.reservation_service import FakeReservationService
from tests.fixtures.factories import event_id, user_id
from tests.integration.messaging.conftest import StartConsumer, eventually

pytestmark = pytest.mark.integration


async def test_published_and_cancelled_messages_are_applied(
    rabbit: BlockingChannel,
    start_consumer: StartConsumer,
    configs: ConfigRepository,
    redis: Redis,
) -> None:
    start_consumer(EVENT_PUBLISHED)
    start_consumer(EVENT_CANCELLED)
    event_service = FakeEventService(rabbit)
    eid = event_id()
    release = await server_now(redis) + timedelta(days=1)

    event_service.publish(messages.event_published(eid, total_capacity=50, release_at=release))
    await eventually(lambda: _status_is(configs, eid, QueueStatus.SCHEDULED))

    event_service.publish(messages.event_cancelled(eid, version=2))
    await eventually(lambda: _status_is(configs, eid, QueueStatus.CANCELLED))


async def test_sold_out_message_is_applied(
    rabbit: BlockingChannel, start_consumer: StartConsumer, configs: ConfigRepository, redis: Redis
) -> None:
    start_consumer(EVENT_PUBLISHED)
    start_consumer(EVENT_SOLD_OUT)
    eid = event_id()
    FakeEventService(rabbit).publish(
        messages.event_published(eid, release_at=await server_now(redis) - timedelta(seconds=1))
    )
    await eventually(lambda: _status_is(configs, eid, QueueStatus.SCHEDULED))

    FakeReservationService(rabbit).publish(messages.event_sold_out(eid))

    await eventually(lambda: _status_is(configs, eid, QueueStatus.CLOSED))


async def test_order_confirmed_admits_the_next_buyer(
    rabbit: BlockingChannel,
    start_consumer: StartConsumer,
    configs: ConfigRepository,
    redis: Redis,
) -> None:
    queues = QueueRepository(redis)  # production deadlines, like the consumer
    start_consumer(EVENT_PUBLISHED)
    start_consumer(ORDER_CONFIRMED)
    eid = event_id()
    FakeEventService(rabbit).publish(
        messages.event_published(
            eid, total_capacity=5, release_at=await server_now(redis) - timedelta(seconds=1)
        )
    )
    await eventually(lambda: _status_is(configs, eid, QueueStatus.SCHEDULED))
    cfg = await configs.get(eid)
    assert cfg is not None
    await queues.shuffle(cfg, Random(0))
    first, second = user_id(), user_id()
    for user in (first, second):
        await queues.join(cfg, user)
    (admission,) = (await queues.admit(cfg)).admitted  # capacity 1: `first`

    FakeCheckoutService(rabbit).publish(
        messages.order_confirmed(admission_id=admission.admission_id, user_id=first, event_id=eid)
    )

    async def second_admitted() -> bool:
        entry = await queues.get_entry(eid, second, await server_now(redis))
        return entry.status is EntryStatus.ADMITTED

    await eventually(second_admitted)


async def test_poison_message_goes_to_the_dlq(
    rabbit: BlockingChannel, start_consumer: StartConsumer
) -> None:
    start_consumer(EVENT_PUBLISHED)

    FakeEventService(rabbit).publish_raw("event.published", b"not json at all")

    await eventually(lambda: _dlq_has(rabbit, EVENT_PUBLISHED.dlq, b"not json at all"))


async def test_failing_handler_is_retried_then_dead_lettered(
    rabbit: BlockingChannel, start_consumer: StartConsumer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(consumers, "RETRY_BASE_DELAY_SECONDS", 0.05)
    calls: list[str] = []

    def always_fails(_: MessageHandlers) -> Handler:
        async def handle(message: Message) -> object:
            calls.append(message.message_id)
            raise ConnectionError("simulated outage")

        return handle

    start_consumer(EVENT_PUBLISHED, handler_override=always_fails)
    msg = messages.event_published(event_id())

    FakeEventService(rabbit).publish(msg)

    await eventually(lambda: _dlq_has(rabbit, EVENT_PUBLISHED.dlq, msg.body))
    assert calls == [msg.message_id] * MAX_ATTEMPTS


async def test_brief_outage_is_ridden_out_with_a_delay_between_attempts(
    rabbit: BlockingChannel, start_consumer: StartConsumer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(consumers, "RETRY_BASE_DELAY_SECONDS", 0.2)
    calls: list[float] = []

    def fails_twice(_: MessageHandlers) -> Handler:
        async def handle(_message: Message) -> object:
            calls.append(time.monotonic())
            if len(calls) <= 2:
                raise ConnectionError("simulated blip")
            return None

        return handle

    start_consumer(EVENT_PUBLISHED, handler_override=fails_twice)
    msg = messages.event_published(event_id())

    FakeEventService(rabbit).publish(msg)

    async def handled() -> bool:
        return len(calls) == 3

    await eventually(handled)
    assert calls[1] - calls[0] >= 0.2
    assert calls[2] - calls[1] >= 0.4
    assert not await _dlq_has(rabbit, EVENT_PUBLISHED.dlq, msg.body)


async def test_waiting_retry_does_not_hold_up_other_messages(
    rabbit: BlockingChannel, start_consumer: StartConsumer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(consumers, "RETRY_BASE_DELAY_SECONDS", 30)  # outlasts the test
    bad, good = messages.event_published(event_id()), messages.event_published(event_id())
    handled: list[str] = []

    def fails_on_bad(_: MessageHandlers) -> Handler:
        async def handle(message: Message) -> object:
            if message.message_id == bad.message_id:
                raise ConnectionError("simulated outage")
            handled.append(message.message_id)
            return None

        return handle

    start_consumer(EVENT_PUBLISHED, handler_override=fails_on_bad)
    publisher = FakeEventService(rabbit)
    publisher.publish(bad)
    publisher.publish(good)

    async def good_was_handled() -> bool:
        return handled == [good.message_id]

    await eventually(good_was_handled)


async def test_consumer_writes_a_heartbeat(start_consumer: StartConsumer, redis: Redis) -> None:
    start_consumer(ORDER_CONFIRMED)

    async def beating() -> bool:
        return bool(await redis.exists(heartbeat_key(ORDER_CONFIRMED)))

    await eventually(beating, within=8)


# --- helpers ---------------------------------------------------------------------------------


async def _status_is(configs: ConfigRepository, eid: str, status: QueueStatus) -> bool:
    cfg = await configs.load(eid)
    return cfg is not None and cfg.status is status


async def _dlq_has(channel: BlockingChannel, dlq: str, body: bytes) -> bool:
    # pika's channel is synchronous; this runs in the test thread, briefly.
    await asyncio.sleep(0)
    method, _, received = channel.basic_get(dlq, auto_ack=True)
    return method is not None and received == body
