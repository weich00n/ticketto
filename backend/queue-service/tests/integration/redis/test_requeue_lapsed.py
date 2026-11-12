"""requeue_lapsed.lua: an admitted buyer past the selection deadline loses the
admission and goes to the back of the queue (short windows: selection 1 s,
admission 2 s)."""

from __future__ import annotations

import asyncio

import pytest
from redis.asyncio import Redis

from app.queue import keys
from app.queue.models import LapseOutcome
from app.queue.repository import QueueRepository
from tests.fixtures.factories import user_id
from tests.integration.redis.conftest import (
    SHORT_EXPIRY_WAIT_S,
    SHORT_SELECTION_S,
    MakeEvent,
    waiting_order,
)

pytestmark = pytest.mark.integration

PAST_SELECTION_WAIT_S = SHORT_SELECTION_S + 0.2


async def test_lapsed_admission_is_removed_and_buyer_requeued_at_the_back(
    redis: Redis, short_repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    lapsed, b, c = user_id(), user_id(), user_id()
    cfg = await admitting_event(capacity=1, users=[lapsed, b, c])
    (adm,) = (await short_repo.admit(cfg)).admitted
    await asyncio.sleep(PAST_SELECTION_WAIT_S)

    result = await short_repo.requeue_lapsed(cfg, lapsed)

    assert (result.outcome, result.admission_id) == (LapseOutcome.REQUEUED, adm.admission_id)
    eid = cfg.event_id
    assert not await redis.exists(keys.admission(eid, adm.admission_id))
    assert await redis.hget(keys.user_admission(eid), lapsed) is None
    assert await redis.zcard(keys.active(eid)) == 0
    assert await redis.hget(keys.ADMISSION_INDEX, adm.admission_id) is None
    assert await waiting_order(redis, cfg) == [b, c, lapsed]
    # The freed slot goes to the head of the queue, not back to the lapsed buyer.
    (nxt,) = (await short_repo.admit(cfg)).admitted
    assert nxt.user_id == b


async def test_before_the_selection_deadline_nothing_changes(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    user, other = user_id(), user_id()
    cfg = await admitting_event(capacity=1, users=[user, other])
    (adm,) = (await repo.admit(cfg)).admitted

    result = await repo.requeue_lapsed(cfg, user)

    assert result.outcome is LapseOutcome.KEPT
    assert await redis.hget(keys.user_admission(cfg.event_id), user) == adm.admission_id
    assert await waiting_order(redis, cfg) == [other]


async def test_buyer_without_admission_is_untouched(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    waiting = user_id()
    cfg = await admitting_event(users=[waiting])

    for user in (waiting, user_id()):
        assert (await repo.requeue_lapsed(cfg, user)).outcome is LapseOutcome.NOT_ADMITTED
    assert await waiting_order(redis, cfg) == [waiting]


async def test_closed_queue_evicts_without_requeueing(
    redis: Redis, short_repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    user = user_id()
    cfg = await admitting_event(capacity=1, users=[user])
    (adm,) = (await short_repo.admit(cfg)).admitted
    await redis.hset(keys.config(cfg.event_id), "status", "CLOSED")
    await asyncio.sleep(PAST_SELECTION_WAIT_S)

    result = await short_repo.requeue_lapsed(cfg, user)

    assert result.outcome is LapseOutcome.EVICTED
    assert not await redis.exists(keys.admission(cfg.event_id, adm.admission_id))
    assert await redis.zcard(keys.active(cfg.event_id)) == 0
    assert await waiting_order(redis, cfg) == []


async def test_admission_past_its_own_deadline_is_left_to_the_sweep(
    redis: Redis, short_repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    user = user_id()
    cfg = await admitting_event(capacity=1, users=[user])
    (adm,) = (await short_repo.admit(cfg)).admitted
    await asyncio.sleep(SHORT_EXPIRY_WAIT_S)

    result = await short_repo.requeue_lapsed(cfg, user)

    assert result.outcome is LapseOutcome.EXPIRED
    assert await redis.zscore(keys.active(cfg.event_id), adm.admission_id) is not None
    assert await waiting_order(redis, cfg) == []
