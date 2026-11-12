from __future__ import annotations

import pytest
from redis.asyncio import Redis

from app.core.status import QueueStatus
from app.core.timefmt import to_ms
from app.queue import keys
from app.queue.models import JoinOutcome, ReleasedAdmission
from app.queue.repository import DRAIN_GRACE, QueueRepository
from tests.fixtures.factories import user_id
from tests.integration.redis.conftest import MakeEvent, set_cached_status

pytestmark = pytest.mark.integration

GRACE_MS = int(DRAIN_GRACE.total_seconds() * 1000)


async def _event_keys(redis: Redis, event_id: str) -> set[str]:
    return {str(k) async for k in redis.scan_iter(match=f"queue:{{{event_id}}}:*")}


async def test_cancel_deletes_everything_but_the_config(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    users = [user_id() for _ in range(5)]
    cfg = await admitting_event(capacity=2, users=users)
    admitted = (await repo.admit(cfg)).admitted
    eid = cfg.event_id
    await redis.sadd(keys.LIVE_EVENTS, eid)
    await redis.sadd(keys.DRAINING_EVENTS, eid)
    await set_cached_status(redis, cfg, QueueStatus.CANCELLED)

    released = await repo.cancel_event(eid)

    assert sorted(released, key=lambda r: r.admission_id) == sorted(
        (ReleasedAdmission(a.admission_id, a.user_id) for a in admitted),
        key=lambda r: r.admission_id,
    )
    assert await _event_keys(redis, eid) == {keys.config(eid)}
    for adm in admitted:
        assert not await redis.hexists(keys.ADMISSION_INDEX, adm.admission_id)
    assert not await redis.sismember(keys.LIVE_EVENTS, eid)
    assert not await redis.sismember(keys.DRAINING_EVENTS, eid)


async def test_cancel_before_release_clears_prequeue(
    redis: Redis, repo: QueueRepository, open_event: MakeEvent
) -> None:
    cfg = await open_event()
    await repo.join(cfg, user_id())

    assert await repo.cancel_event(cfg.event_id) == []
    assert await _event_keys(redis, cfg.event_id) == {keys.config(cfg.event_id)}


async def test_cancel_is_idempotent(repo: QueueRepository, admitting_event: MakeEvent) -> None:
    cfg = await admitting_event(users=[user_id()])
    await repo.admit(cfg)
    await repo.cancel_event(cfg.event_id)
    assert await repo.cancel_event(cfg.event_id) == []


async def test_close_keeps_pool_drops_waiting_and_starts_draining(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    users = [user_id() for _ in range(5)]
    cfg = await admitting_event(capacity=2, users=users)
    admitted = (await repo.admit(cfg)).admitted
    eid = cfg.event_id
    await redis.sadd(keys.LIVE_EVENTS, eid)
    await set_cached_status(redis, cfg, QueueStatus.CLOSED)

    assert await repo.close_event(eid) is True

    assert not await redis.exists(keys.waiting(eid))
    assert not await redis.exists(keys.prequeue(eid))
    assert await redis.zcard(keys.active(eid)) == 2
    assert not await redis.sismember(keys.LIVE_EVENTS, eid)
    assert await redis.sismember(keys.DRAINING_EVENTS, eid)
    last_deadline = max(to_ms(a.admission_expires_at) for a in admitted)
    assert await redis.pexpiretime(keys.active(eid)) == last_deadline + GRACE_MS
    assert await redis.pexpiretime(keys.user_admission(eid)) == last_deadline + GRACE_MS
    for adm in admitted:
        assert await redis.pexpiretime(keys.admission(eid, adm.admission_id)) == (
            to_ms(adm.admission_expires_at) + GRACE_MS
        )


async def test_close_with_empty_pool_does_not_drain(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    cfg = await admitting_event(users=[user_id()])
    await set_cached_status(redis, cfg, QueueStatus.CLOSED)

    assert await repo.close_event(cfg.event_id) is False
    assert not await redis.sismember(keys.DRAINING_EVENTS, cfg.event_id)
    assert not await redis.exists(keys.waiting(cfg.event_id))


async def test_after_close_admitted_keep_admission_and_newcomers_refused(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    admitted_user, waiting_user = user_id(), user_id()
    cfg = await admitting_event(capacity=1, users=[admitted_user, waiting_user])
    (adm,) = (await repo.admit(cfg)).admitted
    await set_cached_status(redis, cfg, QueueStatus.CLOSED)
    await repo.close_event(cfg.event_id)

    assert (await repo.join(cfg, admitted_user)).admission_id == adm.admission_id
    assert (await repo.join(cfg, waiting_user)).outcome is JoinOutcome.CLOSED
    assert (await repo.join(cfg, user_id())).outcome is JoinOutcome.CLOSED
    assert (await repo.admit(cfg)).admitted == []
