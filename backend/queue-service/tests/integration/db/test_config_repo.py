"""ConfigRepository against real MySQL + Redis."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.cursor import InvalidCursor
from app.core.db import QueueConfig
from app.core.status import QueueStatus
from app.core.timefmt import to_ms
from app.queue import keys
from app.queue.config_repo import ConfigRepository, QueueConfigData
from tests.fixtures.factories import DEFAULT_RELEASE, event_id, insert_config

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
def repo(sessions: Sessions, redis: Redis) -> ConfigRepository:
    return ConfigRepository(sessions, redis)


# --- read-through cache --------------------------------------------------------


async def test_unknown_event_is_none_and_not_cached(repo: ConfigRepository, redis: Redis) -> None:
    eid = event_id()
    assert await repo.get(eid) is None
    assert not await redis.exists(keys.config(eid))


async def test_cache_miss_loads_mysql_and_populates_cache(
    repo: ConfigRepository, sessions: Sessions, redis: Redis
) -> None:
    release = DEFAULT_RELEASE + timedelta(milliseconds=250)
    eid = await insert_config(sessions, release_at=release, total_capacity=304, event_version=3)

    cfg = await repo.get(eid)

    assert cfg == QueueConfigData(
        event_id=eid,
        event_version=3,
        total_capacity=304,
        active_pool_capacity=60,
        queue_opens_at=release - timedelta(minutes=15),
        release_at=release,
        status=QueueStatus.SCHEDULED,
        closed_at=None,
    )
    cached = await redis.hgetall(keys.config(eid))
    assert cached["release_at"] == str(to_ms(release))
    assert cached["status"] == "SCHEDULED"
    assert cached["closed_at"] == ""
    # Safety TTL: three days after release.
    expire_at_ms = await redis.pexpiretime(keys.config(eid))
    assert expire_at_ms == to_ms(release + timedelta(days=3))


async def test_cache_hit_does_not_read_mysql(repo: ConfigRepository, sessions: Sessions) -> None:
    eid = await insert_config(sessions)
    await repo.get(eid)
    async with sessions.begin() as session:
        await session.execute(
            update(QueueConfig).where(QueueConfig.event_id == eid).values(total_capacity=999)
        )
    cfg = await repo.get(eid)
    assert cfg is not None and cfg.total_capacity == 304
    loaded = await repo.load(eid)
    assert loaded is not None and loaded.total_capacity == 999


async def test_cache_round_trip_preserves_every_field(
    repo: ConfigRepository, sessions: Sessions
) -> None:
    closed = datetime(2026, 11, 20, 12, 7, 42, 123_000, tzinfo=UTC)
    ends = datetime(2026, 11, 27, 12, 0, 0, 456_000, tzinfo=UTC)
    eid = await insert_config(
        sessions, status=QueueStatus.CLOSED, closed_at=closed, sale_ends_at=ends
    )
    from_db = await repo.load(eid)
    assert from_db is not None and from_db.sale_ends_at == ends
    await repo.get(eid)  # populate
    assert await repo.get(eid) == from_db


async def test_aware_non_utc_datetimes_are_stored_as_utc(
    repo: ConfigRepository, sessions: Sessions
) -> None:
    plus8 = timezone(timedelta(hours=8))
    eid = await insert_config(sessions, release_at=datetime(2026, 11, 20, 20, 0, tzinfo=plus8))
    cfg = await repo.load(eid)
    assert cfg is not None
    assert cfg.release_at == DEFAULT_RELEASE
    assert cfg.release_at.tzinfo is UTC


# --- stale cache writes are refused -------------------------------------------


@pytest.mark.parametrize(
    ("cached", "incoming", "written"),
    [
        (QueueStatus.CLOSED, QueueStatus.OPEN, False),
        (QueueStatus.CLOSED, QueueStatus.ADMITTING, False),
        (QueueStatus.CANCELLED, QueueStatus.ADMITTING, False),
        (QueueStatus.CANCELLED, QueueStatus.CLOSED, False),
        (QueueStatus.CLOSED, QueueStatus.CANCELLED, True),
        (QueueStatus.OPEN, QueueStatus.ADMITTING, True),
        (QueueStatus.OPEN, QueueStatus.CLOSED, True),
    ],
)
async def test_terminal_status_is_sticky_in_cache(
    repo: ConfigRepository,
    sessions: Sessions,
    cached: QueueStatus,
    incoming: QueueStatus,
    written: bool,
) -> None:
    eid = await insert_config(sessions)
    base = await repo.load(eid)
    assert base is not None
    assert await repo.upsert_cache(replace(base, status=cached))

    assert await repo.upsert_cache(replace(base, status=incoming)) is written
    result = await repo.get(eid)
    assert result is not None
    assert result.status is (incoming if written else cached)


async def test_older_version_never_overwrites_newer(
    repo: ConfigRepository, sessions: Sessions
) -> None:
    eid = await insert_config(sessions, event_version=2)
    v2 = await repo.load(eid)
    assert v2 is not None
    assert await repo.upsert_cache(v2)

    assert not await repo.upsert_cache(replace(v2, event_version=1, total_capacity=1))
    assert await repo.upsert_cache(replace(v2, event_version=3, total_capacity=500))
    result = await repo.get(eid)
    assert result is not None and (result.event_version, result.total_capacity) == (3, 500)


# --- status transitions --------------------------------------------------------


async def test_forward_transitions_update_mysql_and_cache(
    repo: ConfigRepository, sessions: Sessions
) -> None:
    eid = await insert_config(sessions)
    await repo.get(eid)  # warm the cache with SCHEDULED

    assert await repo.set_status(eid, QueueStatus.OPEN)
    assert await repo.set_status(eid, QueueStatus.ADMITTING)

    for cfg in (await repo.load(eid), await repo.get(eid)):
        assert cfg is not None and cfg.status is QueueStatus.ADMITTING


async def test_terminal_transition_records_closed_at(
    repo: ConfigRepository, sessions: Sessions
) -> None:
    eid = await insert_config(sessions, status=QueueStatus.ADMITTING)
    sold_out_at = datetime(2026, 11, 20, 12, 7, 42, tzinfo=UTC)
    assert await repo.set_status(eid, QueueStatus.CLOSED, closed_at=sold_out_at)
    cfg = await repo.get(eid)
    assert cfg is not None
    assert (cfg.status, cfg.closed_at) == (QueueStatus.CLOSED, sold_out_at)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (QueueStatus.ADMITTING, QueueStatus.OPEN),  # no going back
        (QueueStatus.OPEN, QueueStatus.OPEN),  # no-op
        (QueueStatus.CLOSED, QueueStatus.ADMITTING),  # admitter racing sold-out
        (QueueStatus.CLOSED, QueueStatus.CLOSED),  # duplicate sold-out
        (QueueStatus.CANCELLED, QueueStatus.CLOSED),
        (QueueStatus.CANCELLED, QueueStatus.CANCELLED),
    ],
)
async def test_disallowed_transitions_change_nothing(
    repo: ConfigRepository, sessions: Sessions, current: QueueStatus, target: QueueStatus
) -> None:
    eid = await insert_config(sessions, status=current)
    assert not await repo.set_status(eid, target)
    cfg = await repo.load(eid)
    assert cfg is not None and cfg.status is current


async def test_sold_out_event_can_still_be_cancelled(
    repo: ConfigRepository, sessions: Sessions
) -> None:
    eid = await insert_config(sessions, status=QueueStatus.CLOSED)
    assert await repo.set_status(eid, QueueStatus.CANCELLED)


async def test_set_status_unknown_event_and_invalid_target(repo: ConfigRepository) -> None:
    assert not await repo.set_status(event_id(), QueueStatus.OPEN)
    with pytest.raises(ValueError):
        await repo.set_status(event_id(), QueueStatus.SCHEDULED)


# --- listing ---------------------------------------------------------------------


async def test_list_pages_in_release_then_event_order(
    repo: ConfigRepository, sessions: Sessions
) -> None:
    same_release = DEFAULT_RELEASE
    expected = []
    for offset_hours in (0, 0, 0, 1, 2):
        release = same_release + timedelta(hours=offset_hours)
        expected.append((release, await insert_config(sessions, release_at=release)))
    expected.sort()

    seen: list[tuple[datetime, str]] = []
    cursor = None
    pages = 0
    while True:
        page = await repo.list_configs(None, 2, cursor)
        seen += [(c.release_at, c.event_id) for c in page.items]
        pages += 1
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert seen == expected
    assert pages == 3


async def test_list_filters_by_status(repo: ConfigRepository, sessions: Sessions) -> None:
    open_ids = {await insert_config(sessions, status=QueueStatus.OPEN) for _ in range(2)}
    await insert_config(sessions, status=QueueStatus.CLOSED)
    page = await repo.list_configs(QueueStatus.OPEN, 50, None)
    assert {c.event_id for c in page.items} == open_ids
    assert page.next_cursor is None


async def test_list_exact_page_has_no_next_cursor(
    repo: ConfigRepository, sessions: Sessions
) -> None:
    for _ in range(2):
        await insert_config(sessions)
    page = await repo.list_configs(None, 2, None)
    assert len(page.items) == 2 and page.next_cursor is None


async def test_list_rejects_bad_cursor_and_limit(repo: ConfigRepository) -> None:
    with pytest.raises(InvalidCursor):
        await repo.list_configs(None, 10, "garbage")
    with pytest.raises(ValueError):
        await repo.list_configs(None, 0, None)
    with pytest.raises(ValueError):
        await repo.list_configs(None, 201, None)


# --- promotion to OPEN -------------------------------------------------------------


async def test_due_for_open_selects_scheduled_whose_queue_has_opened(
    repo: ConfigRepository, sessions: Sessions
) -> None:
    now = DEFAULT_RELEASE - timedelta(minutes=15)
    at_boundary = await insert_config(sessions, release_at=DEFAULT_RELEASE, queue_opens_at=now)
    already_released = await insert_config(sessions, release_at=now - timedelta(hours=1))
    # Event set an early join time: due now although release is 2 h away.
    early = await insert_config(
        sessions,
        release_at=now + timedelta(hours=2),
        queue_opens_at=now - timedelta(minutes=1),
    )
    await insert_config(
        sessions, release_at=DEFAULT_RELEASE, queue_opens_at=now + timedelta(milliseconds=1)
    )
    await insert_config(sessions, release_at=DEFAULT_RELEASE, status=QueueStatus.OPEN)
    await insert_config(sessions, release_at=DEFAULT_RELEASE, status=QueueStatus.CANCELLED)

    due = await repo.due_for_open(now)

    assert [c.event_id for c in due] == [already_released, at_boundary, early]
