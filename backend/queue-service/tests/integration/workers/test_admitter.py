"""Admitter ticks against real Redis and MySQL.

Times are relative to Redis TIME. Sweep tests use the 1 s / 2 s deadlines.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from random import Random

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.redis import server_now
from app.core.status import QueueStatus
from app.core.timefmt import to_ms
from app.queue import keys
from app.queue.admitter import HEARTBEAT_TTL_MS, Admitter
from app.queue.config_repo import ConfigRepository, QueueConfigData
from app.queue.repository import QueueRepository
from tests.fixtures.factories import insert_config, user_id

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
SHORT_EXPIRY_WAIT_S = 2.2


@pytest.fixture
def configs(sessions: Sessions, redis: Redis) -> ConfigRepository:
    return ConfigRepository(sessions, redis)


@pytest.fixture
def queues(redis: Redis) -> QueueRepository:
    return QueueRepository(redis, selection_window_seconds=1, admission_window_seconds=2)


@pytest.fixture
def admitter(configs: ConfigRepository, queues: QueueRepository, redis: Redis) -> Admitter:
    return Admitter(configs, queues, redis, tick_seconds=1, shuffle_rng=Random(0))


async def _event(
    redis: Redis,
    sessions: Sessions,
    configs: ConfigRepository,
    *,
    release_in: timedelta,
    capacity: int = 2,
    status: QueueStatus = QueueStatus.SCHEDULED,
    sale_ends_in: timedelta | None = None,
) -> QueueConfigData:
    now = await server_now(redis)
    eid = await insert_config(
        sessions,
        release_at=now + release_in,
        total_capacity=capacity * 5,
        status=status,
        sale_ends_at=None if sale_ends_in is None else now + sale_ends_in,
    )
    cfg = await configs.load(eid)
    assert cfg is not None
    return cfg


async def _db_status(configs: ConfigRepository, event_id: str) -> QueueStatus:
    cfg = await configs.load(event_id)
    assert cfg is not None
    return cfg.status


# --- promotion -----------------------------------------------------------------------------


async def test_promotes_events_whose_lobby_has_opened(
    admitter: Admitter, redis: Redis, sessions: Sessions, configs: ConfigRepository
) -> None:
    due = await _event(redis, sessions, configs, release_in=timedelta(minutes=10))
    later = await _event(redis, sessions, configs, release_in=timedelta(hours=1))

    report = await admitter.tick()

    assert report.opened == [due.event_id]
    assert await _db_status(configs, due.event_id) is QueueStatus.OPEN
    assert await _db_status(configs, later.event_id) is QueueStatus.SCHEDULED
    assert await redis.smembers(keys.LIVE_EVENTS) == {due.event_id}
    cached = await configs.get(due.event_id)
    assert cached is not None and cached.status is QueueStatus.OPEN


async def test_rebuilds_live_set_from_mysql(
    admitter: Admitter, redis: Redis, sessions: Sessions, configs: ConfigRepository
) -> None:
    """E.g. after a Redis failover lost queue:live_events."""
    cfg = await _event(
        redis, sessions, configs, release_in=-timedelta(minutes=1), status=QueueStatus.ADMITTING
    )

    await admitter.tick()

    assert await redis.sismember(keys.LIVE_EVENTS, cfg.event_id)


# --- release, shuffle, admit ---------------------------------------------------------------


async def test_nothing_admitted_before_release(
    admitter: Admitter,
    redis: Redis,
    sessions: Sessions,
    configs: ConfigRepository,
    queues: QueueRepository,
) -> None:
    cfg = await _event(redis, sessions, configs, release_in=timedelta(minutes=5))
    await admitter.tick()
    await queues.join(cfg, user_id())

    report = await admitter.tick()

    assert (report.shuffled, report.admitted) == ([], {})
    assert not await queues.is_shuffled(cfg.event_id)
    assert await redis.scard(keys.prequeue(cfg.event_id)) == 1


async def test_release_shuffles_once_fills_pool_and_persists_admitting(
    admitter: Admitter,
    redis: Redis,
    sessions: Sessions,
    configs: ConfigRepository,
    queues: QueueRepository,
) -> None:
    cfg = await _event(redis, sessions, configs, release_in=timedelta(seconds=1.5), capacity=2)
    await admitter.tick()  # opens the lobby
    users = [user_id() for _ in range(5)]
    for user in users:
        await queues.join(cfg, user)
    assert (await admitter.tick()).admitted == {}

    now = await server_now(redis)
    await asyncio.sleep(max(0.0, (cfg.release_at - now).total_seconds()) + 0.1)
    report = await admitter.tick()

    assert report.shuffled == [cfg.event_id]
    assert report.admitted == {cfg.event_id: 2}
    assert await _db_status(configs, cfg.event_id) is QueueStatus.ADMITTING
    # Shuffled order = the seeded rng applied to the sorted pre-queue; the first two
    # in that order were admitted, the rest wait in that order.
    order = sorted(users)
    Random(0).shuffle(order)
    now = await server_now(redis)
    statuses = [(await queues.get_entry(cfg.event_id, u, now)) for u in order]
    assert [e.status.value for e in statuses] == ["ADMITTED"] * 2 + ["WAITING"] * 3
    assert [e.position for e in statuses[2:]] == [1, 2, 3]
    second = await admitter.tick()
    assert (second.shuffled, second.admitted) == ([], {})


async def test_event_released_while_admitter_was_down(
    admitter: Admitter,
    redis: Redis,
    sessions: Sessions,
    configs: ConfigRepository,
    queues: QueueRepository,
) -> None:
    """Still SCHEDULED in MySQL, release already past: one tick catches up."""
    cfg = await _event(redis, sessions, configs, release_in=-timedelta(seconds=30), capacity=1)
    await queues.join(cfg, user_id())

    report = await admitter.tick()

    assert report.opened == [cfg.event_id]
    assert report.shuffled == [cfg.event_id]
    assert report.admitted == {cfg.event_id: 1}
    assert await _db_status(configs, cfg.event_id) is QueueStatus.ADMITTING


async def test_expiry_sweep_refills_fifo(
    admitter: Admitter,
    redis: Redis,
    sessions: Sessions,
    configs: ConfigRepository,
    queues: QueueRepository,
) -> None:
    cfg = await _event(
        redis, sessions, configs, release_in=-timedelta(seconds=1), status=QueueStatus.ADMITTING
    )
    await admitter.tick()  # shuffles an empty pre-queue
    users = [user_id() for _ in range(4)]
    for user in users:
        await queues.join(cfg, user)
    assert (await admitter.tick()).admitted == {cfg.event_id: 2}

    await asyncio.sleep(SHORT_EXPIRY_WAIT_S)
    report = await admitter.tick()

    assert report.released == {cfg.event_id: 2}
    assert report.admitted == {cfg.event_id: 2}
    now = await server_now(redis)
    for user in users[2:]:
        assert (await queues.get_entry(cfg.event_id, user, now)).status.value == "ADMITTED"


# --- terminal events ---------------------------------------------------------------------


async def test_terminal_event_leaves_the_work_list(
    admitter: Admitter,
    redis: Redis,
    sessions: Sessions,
    configs: ConfigRepository,
    queues: QueueRepository,
) -> None:
    cfg = await _event(
        redis, sessions, configs, release_in=-timedelta(seconds=1), status=QueueStatus.ADMITTING
    )
    await admitter.tick()
    await queues.join(cfg, user_id())
    await configs.set_status(cfg.event_id, QueueStatus.CLOSED)  # handler hasn't cleaned up

    report = await admitter.tick()

    assert report.dropped_live == [cfg.event_id]
    assert report.admitted == {}
    assert not await redis.sismember(keys.LIVE_EVENTS, cfg.event_id)


async def test_draining_event_is_swept_until_empty(
    admitter: Admitter,
    redis: Redis,
    sessions: Sessions,
    configs: ConfigRepository,
    queues: QueueRepository,
) -> None:
    cfg = await _event(
        redis, sessions, configs, release_in=-timedelta(seconds=1), status=QueueStatus.ADMITTING
    )
    await admitter.tick()
    await queues.join(cfg, user_id())
    await admitter.tick()
    assert await queues.pool_size(cfg.event_id) == 1
    await configs.set_status(cfg.event_id, QueueStatus.CLOSED)
    assert await queues.close_event(cfg.event_id)  # now draining

    first = await admitter.tick()
    assert first.drained == []
    assert await redis.sismember(keys.DRAINING_EVENTS, cfg.event_id)

    await asyncio.sleep(SHORT_EXPIRY_WAIT_S)
    report = await admitter.tick()

    assert report.released == {cfg.event_id: 1}
    assert report.drained == [cfg.event_id]
    assert not await redis.sismember(keys.DRAINING_EVENTS, cfg.event_id)


# --- robustness -------------------------------------------------------------------------------


async def test_one_broken_event_does_not_stop_the_others(
    admitter: Admitter,
    redis: Redis,
    sessions: Sessions,
    configs: ConfigRepository,
    queues: QueueRepository,
) -> None:
    good = await _event(
        redis, sessions, configs, release_in=-timedelta(seconds=1), status=QueueStatus.ADMITTING
    )
    broken = await _event(
        redis, sessions, configs, release_in=-timedelta(seconds=1), status=QueueStatus.ADMITTING
    )
    await admitter.tick()
    await queues.join(good, user_id())
    await redis.hset(keys.config(broken.event_id), "release_at", "not-a-number")

    report = await admitter.tick()

    assert report.failed == [broken.event_id]
    assert report.admitted == {good.event_id: 1}


async def test_heartbeat(admitter: Admitter, redis: Redis) -> None:
    await admitter.tick()

    beat = int(await redis.get(keys.ADMITTER_HEARTBEAT) or 0)
    ttl = await redis.pttl(keys.ADMITTER_HEARTBEAT)

    assert abs(beat - to_ms(await server_now(redis))) < 2_000
    assert 0 < ttl <= HEARTBEAT_TTL_MS


# --- end of sale (sale_ends_at) -----------------------------------------------------------


async def test_sale_end_closes_the_queue_and_drains(
    admitter: Admitter,
    redis: Redis,
    sessions: Sessions,
    configs: ConfigRepository,
    queues: QueueRepository,
) -> None:
    cfg = await _event(
        redis,
        sessions,
        configs,
        release_in=-timedelta(seconds=1),
        status=QueueStatus.ADMITTING,
        capacity=1,
        sale_ends_in=timedelta(seconds=1),
    )
    await admitter.tick()  # shuffles the empty pre-queue
    admitted, waiting = user_id(), user_id()
    for user in (admitted, waiting):
        await queues.join(cfg, user)
    assert (await admitter.tick()).admitted == {cfg.event_id: 1}

    await asyncio.sleep(1.1)  # the sale ends
    report = await admitter.tick()

    assert report.closed == [cfg.event_id]
    assert report.admitted == {}
    closed = await configs.load(cfg.event_id)
    assert closed is not None
    assert (closed.status, closed.closed_at) == (QueueStatus.CLOSED, cfg.sale_ends_at)
    assert not await redis.exists(keys.waiting(cfg.event_id))
    assert not await redis.sismember(keys.LIVE_EVENTS, cfg.event_id)
    assert await redis.sismember(keys.DRAINING_EVENTS, cfg.event_id)
    now = await server_now(redis)
    assert (await queues.get_entry(cfg.event_id, admitted, now)).status.value == "ADMITTED"

    await asyncio.sleep(SHORT_EXPIRY_WAIT_S)  # the remaining admission expires
    assert (await admitter.tick()).drained == [cfg.event_id]


async def test_event_whose_sale_ended_while_admitter_was_down(
    admitter: Admitter, redis: Redis, sessions: Sessions, configs: ConfigRepository
) -> None:
    """Still SCHEDULED in MySQL, and both release and sale end are in the past."""
    cfg = await _event(
        redis,
        sessions,
        configs,
        release_in=-timedelta(hours=2),
        sale_ends_in=-timedelta(hours=1),
    )

    report = await admitter.tick()

    assert report.opened == []  # never opened: straight to CLOSED
    assert report.closed == [cfg.event_id]
    closed = await configs.load(cfg.event_id)
    assert closed is not None and closed.status is QueueStatus.CLOSED


async def test_event_with_a_future_sale_end_keeps_admitting(
    admitter: Admitter,
    redis: Redis,
    sessions: Sessions,
    configs: ConfigRepository,
    queues: QueueRepository,
) -> None:
    cfg = await _event(
        redis,
        sessions,
        configs,
        release_in=-timedelta(seconds=1),
        status=QueueStatus.ADMITTING,
        sale_ends_in=timedelta(days=7),
    )
    await admitter.tick()
    await queues.join(cfg, user_id())

    report = await admitter.tick()

    assert (report.closed, report.admitted) == ([], {cfg.event_id: 1})
