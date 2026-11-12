from __future__ import annotations

import asyncio

import pytest
from redis.asyncio import Redis

from app.core.redis import server_now
from app.queue import keys
from app.queue.models import LeaveOutcome
from app.queue.repository import QueueRepository
from tests.fixtures.factories import user_id
from tests.integration.redis.conftest import SHORT_EXPIRY_WAIT_S, MakeEvent, waiting_order

pytestmark = pytest.mark.integration


async def test_waiting_buyer_leaves_and_others_move_up(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    a, b, c = user_id(), user_id(), user_id()
    cfg = await admitting_event(users=[a, b, c])

    assert await repo.leave(cfg.event_id, a) is LeaveOutcome.LEFT

    assert await waiting_order(redis, cfg) == [b, c]
    entry = await repo.get_entry(cfg.event_id, c, await server_now(redis))
    assert entry.position == 2


async def test_prequeue_buyer_leaves(
    redis: Redis, repo: QueueRepository, open_event: MakeEvent
) -> None:
    cfg = await open_event()
    user = user_id()
    await repo.join(cfg, user)

    assert await repo.leave(cfg.event_id, user) is LeaveOutcome.LEFT
    assert not await redis.sismember(keys.prequeue(cfg.event_id), user)


async def test_admitted_buyer_cannot_leave(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    user = user_id()
    cfg = await admitting_event(users=[user])
    (adm,) = (await repo.admit(cfg)).admitted

    assert await repo.leave(cfg.event_id, user) is LeaveOutcome.ALREADY_ADMITTED
    assert await redis.zscore(keys.active(cfg.event_id), adm.admission_id) is not None


async def test_unknown_buyer_is_not_in_queue(
    repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    cfg = await admitting_event()
    assert await repo.leave(cfg.event_id, user_id()) is LeaveOutcome.NOT_IN_QUEUE


async def test_leaving_twice_is_not_in_queue(
    repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    user = user_id()
    cfg = await admitting_event(users=[user])
    await repo.leave(cfg.event_id, user)
    assert await repo.leave(cfg.event_id, user) is LeaveOutcome.NOT_IN_QUEUE


async def test_expired_admission_no_longer_counts_as_admitted(
    short_repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    user = user_id()
    cfg = await admitting_event(users=[user])
    await short_repo.admit(cfg)
    await asyncio.sleep(SHORT_EXPIRY_WAIT_S)

    assert await short_repo.leave(cfg.event_id, user) is LeaveOutcome.NOT_IN_QUEUE
