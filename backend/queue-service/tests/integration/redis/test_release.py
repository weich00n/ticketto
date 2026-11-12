from __future__ import annotations

import asyncio

import pytest
from redis.asyncio import Redis

from app.queue import keys
from app.queue.config_repo import QueueConfigData
from app.queue.models import Admission, ReleaseOutcome
from app.queue.repository import QueueRepository
from tests.fixtures.factories import event_id, user_id
from tests.integration.redis.conftest import SHORT_EXPIRY_WAIT_S, MakeEvent

pytestmark = pytest.mark.integration


async def _snapshot(redis: Redis, cfg: QueueConfigData, adm: Admission) -> tuple[object, ...]:
    eid = cfg.event_id
    return (
        await redis.hgetall(keys.admission(eid, adm.admission_id)),
        await redis.hgetall(keys.user_admission(eid)),
        await redis.zrange(keys.active(eid), 0, -1, withscores=True),
        await redis.hget(keys.ADMISSION_INDEX, adm.admission_id),
    )


async def test_release_removes_admission_everywhere(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    user = user_id()
    cfg = await admitting_event(users=[user])
    (adm,) = (await repo.admit(cfg)).admitted

    result = await repo.release(adm.admission_id, user, cfg.event_id)

    assert result.outcome is ReleaseOutcome.RELEASED
    eid = cfg.event_id
    assert not await redis.exists(keys.admission(eid, adm.admission_id))
    assert await redis.hget(keys.user_admission(eid), user) is None
    assert await redis.zcard(keys.active(eid)) == 0
    assert await redis.hget(keys.ADMISSION_INDEX, adm.admission_id) is None


async def test_unknown_admission_is_a_no_op(
    repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    cfg = await admitting_event()
    result = await repo.release(str(user_id()), user_id(), cfg.event_id)
    assert result.outcome is ReleaseOutcome.NOT_FOUND


async def test_second_release_is_not_found(
    repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    user = user_id()
    cfg = await admitting_event(users=[user])
    (adm,) = (await repo.admit(cfg)).admitted
    await repo.release(adm.admission_id, user, cfg.event_id)

    again = await repo.release(adm.admission_id, user, cfg.event_id)

    assert again.outcome is ReleaseOutcome.NOT_FOUND


async def test_user_mismatch_changes_nothing(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    user = user_id()
    cfg = await admitting_event(users=[user])
    (adm,) = (await repo.admit(cfg)).admitted
    before = await _snapshot(redis, cfg, adm)

    result = await repo.release(adm.admission_id, user_id(), cfg.event_id)

    assert (result.outcome, result.stored_user_id, result.stored_event_id) == (
        ReleaseOutcome.MISMATCH,
        user,
        cfg.event_id,
    )
    assert await _snapshot(redis, cfg, adm) == before


async def test_event_mismatch_changes_nothing(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    user = user_id()
    cfg = await admitting_event(users=[user])
    (adm,) = (await repo.admit(cfg)).admitted
    before = await _snapshot(redis, cfg, adm)

    result = await repo.release(adm.admission_id, user, event_id())

    assert (result.outcome, result.stored_user_id, result.stored_event_id) == (
        ReleaseOutcome.MISMATCH,
        user,
        cfg.event_id,
    )
    assert await _snapshot(redis, cfg, adm) == before


async def test_expired_admission_is_left_to_the_sweep(
    redis: Redis, short_repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    user = user_id()
    cfg = await admitting_event(users=[user])
    (adm,) = (await short_repo.admit(cfg)).admitted
    await asyncio.sleep(SHORT_EXPIRY_WAIT_S)
    before = await _snapshot(redis, cfg, adm)

    result = await short_repo.release(adm.admission_id, user, cfg.event_id)

    assert result.outcome is ReleaseOutcome.EXPIRED
    assert await _snapshot(redis, cfg, adm) == before
    swept = await short_repo.admit(cfg)
    assert [r.admission_id for r in swept.released] == [adm.admission_id]
