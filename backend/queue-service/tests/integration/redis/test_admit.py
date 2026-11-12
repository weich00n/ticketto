from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from redis.asyncio import Redis

from app.core.status import QueueStatus
from app.core.timefmt import to_ms
from app.queue import keys
from app.queue.models import ReleaseOutcome
from app.queue.repository import QueueRepository
from tests.fixtures.factories import user_id
from tests.integration.redis.conftest import (
    SHORT_ADMISSION_S,
    SHORT_EXPIRY_WAIT_S,
    SHORT_SELECTION_S,
    MakeEvent,
    set_cached_status,
    waiting_order,
)

pytestmark = pytest.mark.integration


async def test_nothing_admitted_before_release(
    redis: Redis, repo: QueueRepository, open_event: MakeEvent
) -> None:
    cfg = await open_event()
    await repo.join(cfg, user_id())

    result = await repo.admit(cfg)

    assert (result.admitted, result.released) == ([], [])
    assert await redis.scard(keys.prequeue(cfg.event_id)) == 1


async def test_release_fills_pool_to_capacity_in_fifo_order(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    users = [user_id() for _ in range(5)]
    cfg = await admitting_event(capacity=2, users=users)

    result = await repo.admit(cfg)

    assert [a.user_id for a in result.admitted] == users[:2]
    assert await waiting_order(redis, cfg) == users[2:]
    assert await redis.zcard(keys.active(cfg.event_id)) == 2


async def test_admission_records_deadlines_and_index(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    user = user_id()
    cfg = await admitting_event(users=[user])

    result = await repo.admit(cfg)
    (adm,) = result.admitted

    assert adm.event_id == cfg.event_id and adm.user_id == user
    assert adm.admitted_at == result.now
    assert adm.selection_expires_at == adm.admitted_at + timedelta(minutes=5)
    assert adm.admission_expires_at == adm.admitted_at + timedelta(minutes=11)
    eid = cfg.event_id
    stored = await redis.hgetall(keys.admission(eid, adm.admission_id))
    assert stored == {
        "admission_id": adm.admission_id,
        "user_id": user,
        "event_id": eid,
        "admitted_at": str(to_ms(adm.admitted_at)),
        "selection_expires_at": str(to_ms(adm.selection_expires_at)),
        "admission_expires_at": str(to_ms(adm.admission_expires_at)),
    }
    assert await redis.hget(keys.user_admission(eid), user) == adm.admission_id
    assert await redis.zscore(keys.active(eid), adm.admission_id) == to_ms(adm.admission_expires_at)
    assert await redis.hget(keys.ADMISSION_INDEX, adm.admission_id) == eid
    assert await redis.pexpiretime(keys.admission(eid, adm.admission_id)) > to_ms(
        adm.admission_expires_at
    )


async def test_full_pool_admits_nobody(repo: QueueRepository, admitting_event: MakeEvent) -> None:
    cfg = await admitting_event(capacity=2, users=[user_id() for _ in range(4)])
    await repo.admit(cfg)

    for _ in range(3):
        assert (await repo.admit(cfg)).admitted == []


async def test_each_freed_slot_admits_exactly_one(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    users = [user_id() for _ in range(5)]
    cfg = await admitting_event(capacity=2, users=users)
    first, _second = (await repo.admit(cfg)).admitted

    release = await repo.release(first.admission_id, first.user_id, cfg.event_id)
    assert release.outcome is ReleaseOutcome.RELEASED
    result = await repo.admit(cfg)

    assert [a.user_id for a in result.admitted] == [users[2]]
    assert await waiting_order(redis, cfg) == users[3:]


async def test_empty_queue_admits_nobody(repo: QueueRepository, admitting_event: MakeEvent) -> None:
    cfg = await admitting_event(capacity=3)
    assert (await repo.admit(cfg)).admitted == []


async def test_sweep_frees_expired_and_admits_next_fifo(
    redis: Redis, short_repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    users = [user_id() for _ in range(4)]
    cfg = await admitting_event(capacity=2, users=users)
    old = (await short_repo.admit(cfg)).admitted
    assert [a.selection_expires_at - a.admitted_at for a in old] == [
        timedelta(seconds=SHORT_SELECTION_S)
    ] * 2
    assert old[0].admission_expires_at - old[0].admitted_at == timedelta(seconds=SHORT_ADMISSION_S)

    await asyncio.sleep(SHORT_EXPIRY_WAIT_S)
    result = await short_repo.admit(cfg)

    assert {(r.admission_id, r.user_id) for r in result.released} == {
        (a.admission_id, a.user_id) for a in old
    }
    assert [a.user_id for a in result.admitted] == users[2:]
    eid = cfg.event_id
    for adm in old:
        assert not await redis.exists(keys.admission(eid, adm.admission_id))
        assert not await redis.hexists(keys.ADMISSION_INDEX, adm.admission_id)
        assert await redis.hget(keys.user_admission(eid), adm.user_id) is None


@pytest.mark.parametrize("status", [QueueStatus.CLOSED, QueueStatus.CANCELLED])
async def test_terminal_status_sweeps_but_never_admits(
    redis: Redis,
    short_repo: QueueRepository,
    admitting_event: MakeEvent,
    status: QueueStatus,
) -> None:
    cfg = await admitting_event(capacity=1, users=[user_id(), user_id()])
    (old,) = (await short_repo.admit(cfg)).admitted
    await set_cached_status(redis, cfg, status)
    await asyncio.sleep(SHORT_EXPIRY_WAIT_S)

    result = await short_repo.admit(cfg)

    assert [r.admission_id for r in result.released] == [old.admission_id]
    assert result.admitted == []
    assert await redis.zcard(keys.waiting(cfg.event_id)) == 1
