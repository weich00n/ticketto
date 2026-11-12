from __future__ import annotations

import asyncio

import pytest
from redis.asyncio import Redis

from app.core.cursor import InvalidCursor
from app.core.redis import server_now
from app.core.status import EntryStatus
from app.queue import keys
from app.queue.repository import QueueRepository
from tests.fixtures.factories import user_id
from tests.integration.redis.conftest import SHORT_EXPIRY_WAIT_S, MakeEvent

pytestmark = pytest.mark.integration


# --- get_entry -------------------------------------------------------------------


async def test_entry_states(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent, open_event: MakeEvent
) -> None:
    admitted_user, waiting_user = user_id(), user_id()
    cfg = await admitting_event(capacity=1, users=[admitted_user, waiting_user])
    (adm,) = (await repo.admit(cfg)).admitted
    pre_cfg = await open_event()
    pre_user = user_id()
    await repo.join(pre_cfg, pre_user)
    now = await server_now(redis)

    admitted = await repo.get_entry(cfg.event_id, admitted_user, now)
    assert (admitted.status, admitted.admission) == (EntryStatus.ADMITTED, adm)

    waiting = await repo.get_entry(cfg.event_id, waiting_user, now)
    assert (waiting.status, waiting.position) == (EntryStatus.WAITING, 1)

    pre = await repo.get_entry(pre_cfg.event_id, pre_user, now)
    assert (pre.status, pre.position) == (EntryStatus.WAITING, None)

    nobody = await repo.get_entry(cfg.event_id, user_id(), now)
    assert nobody.status is EntryStatus.NONE


async def test_expired_admission_reads_as_none_before_sweep(
    redis: Redis, short_repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    user = user_id()
    cfg = await admitting_event(users=[user])
    await short_repo.admit(cfg)
    await asyncio.sleep(SHORT_EXPIRY_WAIT_S)

    entry = await short_repo.get_entry(cfg.event_id, user, await server_now(redis))

    assert entry.status is EntryStatus.NONE


# --- counts ----------------------------------------------------------------------


async def test_counts_include_prequeue_and_exclude_expired(
    redis: Redis, short_repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    cfg = await admitting_event(capacity=2, users=[user_id() for _ in range(5)])
    await short_repo.admit(cfg)
    await redis.sadd(keys.prequeue(cfg.event_id), user_id())

    counts = await short_repo.counts(cfg.event_id, await server_now(redis))
    assert (counts.waiting, counts.active) == (4, 2)

    await asyncio.sleep(SHORT_EXPIRY_WAIT_S)
    counts = await short_repo.counts(cfg.event_id, await server_now(redis))
    assert counts.active == 0


# --- get_admission ---------------------------------------------------------------


async def test_get_admission_by_id(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    user = user_id()
    cfg = await admitting_event(users=[user])
    (adm,) = (await repo.admit(cfg)).admitted
    now = await server_now(redis)

    assert await repo.get_admission(adm.admission_id, now) == adm

    await repo.release(adm.admission_id, user, cfg.event_id)
    assert await repo.get_admission(adm.admission_id, now) is None
    assert await repo.get_admission(user_id(), now) is None


async def test_get_admission_drops_stale_index_entry(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    cfg = await admitting_event(users=[user_id()])
    (adm,) = (await repo.admit(cfg)).admitted
    await redis.delete(keys.admission(cfg.event_id, adm.admission_id))  # e.g. TTL expired

    assert await repo.get_admission(adm.admission_id, await server_now(redis)) is None
    assert not await redis.hexists(keys.ADMISSION_INDEX, adm.admission_id)


async def test_get_admission_hides_expired(
    redis: Redis, short_repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    cfg = await admitting_event(users=[user_id()])
    (adm,) = (await short_repo.admit(cfg)).admitted
    await asyncio.sleep(SHORT_EXPIRY_WAIT_S)

    assert await short_repo.get_admission(adm.admission_id, await server_now(redis)) is None


# --- list_admissions ---------------------------------------------------------------


async def test_list_admissions_pages_without_gaps_or_duplicates(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    """Seven admissions from one admit call share a score: ties break by id."""
    cfg = await admitting_event(capacity=7, users=[user_id() for _ in range(7)])
    first_batch = (await repo.admit(cfg)).admitted
    # A later admission with a later deadline.
    released = first_batch[0]
    await repo.release(released.admission_id, released.user_id, cfg.event_id)
    await repo.join(cfg, user_id())
    (late,) = (await repo.admit(cfg)).admitted
    now = await server_now(redis)

    seen = []
    cursor = None
    while True:
        page = await repo.list_admissions(cfg.event_id, now, 3, cursor)
        seen += page.items
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    expected = [*sorted(first_batch[1:], key=lambda a: a.admission_id), late]
    assert seen == expected


async def test_list_admissions_skips_expired(
    redis: Redis, short_repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    cfg = await admitting_event(capacity=2, users=[user_id(), user_id()])
    await short_repo.admit(cfg)
    await asyncio.sleep(SHORT_EXPIRY_WAIT_S)

    page = await short_repo.list_admissions(cfg.event_id, await server_now(redis), 50, None)

    assert (page.items, page.next_cursor) == ([], None)


async def test_list_admissions_rejects_bad_input(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    cfg = await admitting_event()
    now = await server_now(redis)
    with pytest.raises(InvalidCursor):
        await repo.list_admissions(cfg.event_id, now, 10, "garbage")
    with pytest.raises(ValueError):
        await repo.list_admissions(cfg.event_id, now, 0, None)
    with pytest.raises(ValueError):
        await repo.list_admissions(cfg.event_id, now, 201, None)
