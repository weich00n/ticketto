from __future__ import annotations

import asyncio

import pytest
from redis.asyncio import Redis

from app.core.redis import server_now
from app.core.status import EntryStatus, QueueStatus
from app.queue import keys
from app.queue.models import JoinOutcome
from app.queue.repository import QueueRepository
from tests.fixtures.factories import user_id
from tests.integration.redis.conftest import (
    SHORT_EXPIRY_WAIT_S,
    MakeEvent,
    set_cached_status,
    waiting_order,
)

pytestmark = pytest.mark.integration


async def test_pre_release_join_goes_to_unordered_prequeue(
    redis: Redis, repo: QueueRepository, open_event: MakeEvent
) -> None:
    cfg = await open_event()
    user = user_id()

    result = await repo.join(cfg, user)

    assert result.outcome is JoinOutcome.CREATED
    assert await redis.sismember(keys.prequeue(cfg.event_id), user)
    assert not await redis.exists(keys.waiting(cfg.event_id))
    assert await redis.pexpiretime(keys.prequeue(cfg.event_id)) > 0  # safety TTL set


async def test_repeat_pre_release_join_changes_nothing(
    redis: Redis, repo: QueueRepository, open_event: MakeEvent
) -> None:
    cfg = await open_event()
    user = user_id()
    await repo.join(cfg, user)

    for _ in range(3):
        assert (await repo.join(cfg, user)).outcome is JoinOutcome.ALREADY_WAITING
    assert await redis.scard(keys.prequeue(cfg.event_id)) == 1


async def test_post_release_joins_are_fifo(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    users = [user_id() for _ in range(5)]
    cfg = await admitting_event(users=users)

    assert await waiting_order(redis, cfg) == users
    now = await server_now(redis)
    for position, user in enumerate(users, start=1):
        entry = await repo.get_entry(cfg.event_id, user, now)
        assert (entry.status, entry.position) == (EntryStatus.WAITING, position)


async def test_repeat_post_release_join_keeps_position(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    first, second = user_id(), user_id()
    cfg = await admitting_event(users=[first, second])

    assert (await repo.join(cfg, first)).outcome is JoinOutcome.ALREADY_WAITING
    assert await waiting_order(redis, cfg) == [first, second]
    assert await redis.get(keys.seq(cfg.event_id)) == "2"


@pytest.mark.parametrize("status", [QueueStatus.CLOSED, QueueStatus.CANCELLED])
async def test_terminal_status_refuses_new_entries(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent, status: QueueStatus
) -> None:
    cfg = await admitting_event()
    await set_cached_status(redis, cfg, status)

    result = await repo.join(cfg, user_id())

    assert (result.outcome, result.closed_status) == (JoinOutcome.CLOSED, status.value)
    assert not await redis.exists(keys.waiting(cfg.event_id))


async def test_admitted_buyer_gets_same_admission_and_no_new_timer(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    user = user_id()
    cfg = await admitting_event(users=[user])
    (admission,) = (await repo.admit(cfg)).admitted
    hash_before = await redis.hgetall(keys.admission(cfg.event_id, admission.admission_id))

    result = await repo.join(cfg, user)

    assert (result.outcome, result.admission_id) == (
        JoinOutcome.ALREADY_ADMITTED,
        admission.admission_id,
    )
    assert await redis.hgetall(keys.admission(cfg.event_id, admission.admission_id)) == hash_before
    assert not await redis.exists(keys.waiting(cfg.event_id))


async def test_admitted_buyer_keeps_admission_after_sold_out(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    user = user_id()
    cfg = await admitting_event(users=[user])
    (admission,) = (await repo.admit(cfg)).admitted
    await set_cached_status(redis, cfg, QueueStatus.CLOSED)

    result = await repo.join(cfg, user)

    assert (result.outcome, result.admission_id) == (
        JoinOutcome.ALREADY_ADMITTED,
        admission.admission_id,
    )
    assert (await repo.join(cfg, user_id())).outcome is JoinOutcome.CLOSED


async def test_rejoin_after_admission_expired_goes_to_back(
    redis: Redis, short_repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    first, second = user_id(), user_id()
    cfg = await admitting_event(capacity=1, users=[first, second])
    (old,) = (await short_repo.admit(cfg)).admitted
    await asyncio.sleep(SHORT_EXPIRY_WAIT_S)  # deadline passes, no sweep has run

    result = await short_repo.join(cfg, first)

    assert result.outcome is JoinOutcome.CREATED
    assert await waiting_order(redis, cfg) == [second, first]
    assert not await redis.exists(keys.admission(cfg.event_id, old.admission_id))
    assert not await redis.hexists(keys.user_admission(cfg.event_id), first)
    assert await redis.zscore(keys.active(cfg.event_id), old.admission_id) is None
    assert not await redis.hexists(keys.ADMISSION_INDEX, old.admission_id)
