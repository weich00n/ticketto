"""Every rule in CLAUDE.md's "Consumed messages" section, handlers called directly
with real envelopes (tests/fakes/messages.py) against real MySQL and Redis."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from random import Random

import pytest
from redis.asyncio import Redis

from app.core.redis import server_now
from app.core.status import QueueStatus
from app.messaging.handlers import MessageHandlers, Outcome
from app.queue import keys
from app.queue.config_repo import ConfigRepository, QueueConfigData
from app.queue.models import Admission, JoinOutcome
from app.queue.repository import QueueRepository
from tests.fakes import messages
from tests.fixtures.factories import event_id, user_id
from tests.integration.messaging.conftest import Sessions, deliver

pytestmark = pytest.mark.integration


async def _published(
    handlers: MessageHandlers,
    redis: Redis,
    configs: ConfigRepository,
    *,
    release_in: timedelta,
    capacity: int = 2,
    sale_ends_in: timedelta | None = None,
) -> QueueConfigData:
    now = await server_now(redis)
    eid = event_id()
    await deliver(
        handlers,
        messages.event_published(
            eid,
            total_capacity=capacity * 5,
            release_at=now + release_in,
            sale_ends_at=None if sale_ends_in is None else now + sale_ends_in,
        ),
    )
    cfg = await configs.load(eid)
    assert cfg is not None
    return cfg


async def _admitting(
    handlers: MessageHandlers,
    redis: Redis,
    configs: ConfigRepository,
    queues: QueueRepository,
    *,
    capacity: int = 1,
    users: int = 2,
) -> tuple[QueueConfigData, list[str], list[Admission]]:
    """Released, shuffled, `users` joined FIFO and the pool filled."""
    cfg = await _published(
        handlers, redis, configs, release_in=-timedelta(seconds=1), capacity=capacity
    )
    await queues.shuffle(cfg, Random(0))
    joined = [user_id() for _ in range(users)]
    for user in joined:
        await queues.join(cfg, user)
    admitted = (await queues.admit(cfg)).admitted
    return cfg, joined, admitted


# --- EventPublished ---------------------------------------------------------------------------


async def test_published_creates_scheduled_config(
    handlers: MessageHandlers, redis: Redis, configs: ConfigRepository, sessions: Sessions
) -> None:
    now = await server_now(redis)
    eid = event_id()
    msg = messages.event_published(
        eid,
        total_capacity=304,
        release_at=now + timedelta(days=1),
        queue_join_opens_at=now + timedelta(hours=20),
        sale_ends_at=now + timedelta(days=8),
    )

    result = await deliver(handlers, msg)

    assert result.outcome is Outcome.CREATED
    cfg = await configs.load(eid)
    assert cfg is not None
    assert (cfg.status, cfg.event_version, cfg.total_capacity, cfg.active_pool_capacity) == (
        QueueStatus.SCHEDULED,
        1,
        304,
        60,
    )
    assert cfg.queue_opens_at == cfg.release_at - timedelta(hours=4)  # as sent
    assert cfg.sale_ends_at is not None
    assert await redis.hget(keys.config(eid), "status") == "SCHEDULED"  # cached
    # No queue keys yet: they appear with the first join.
    assert [k async for k in redis.scan_iter(match=f"queue:{{{eid}}}:*")] == [keys.config(eid)]


async def test_duplicate_published_changes_nothing(
    handlers: MessageHandlers, configs: ConfigRepository, sessions: Sessions
) -> None:
    eid = event_id()
    msg = messages.event_published(eid, total_capacity=100)
    await deliver(handlers, msg)

    again = await deliver(handlers, msg)
    other = await deliver(handlers, messages.event_published(eid, total_capacity=999))

    assert (again.outcome, other.outcome) == (Outcome.STALE, Outcome.STALE)
    cfg = await configs.load(eid)
    assert cfg is not None and cfg.total_capacity == 100


# --- EventUpdated -----------------------------------------------------------------------------


async def test_update_while_scheduled_recomputes_and_refreshes_cache(
    handlers: MessageHandlers, redis: Redis, configs: ConfigRepository
) -> None:
    cfg = await _published(
        handlers, redis, configs, release_in=timedelta(days=1), sale_ends_in=timedelta(days=8)
    )
    await configs.get(cfg.event_id)  # warm cache with v1
    new_release = cfg.release_at + timedelta(days=1)

    result = await deliver(
        handlers,
        messages.event_updated(
            cfg.event_id,
            version=2,
            total_capacity=3,
            release_at=new_release,
            queue_join_opens_at=new_release - timedelta(hours=1),
        ),  # no starts_at: full current values, so sale_ends_at is cleared
    )

    assert result.outcome is Outcome.UPDATED
    for updated in (await configs.load(cfg.event_id), await configs.get(cfg.event_id)):
        assert updated is not None
        assert (updated.event_version, updated.total_capacity, updated.active_pool_capacity) == (
            2,
            3,
            1,
        )
        assert updated.release_at == new_release
        assert updated.queue_opens_at == new_release - timedelta(hours=1)
        assert updated.sale_ends_at is None


async def test_stale_update_is_ignored(
    handlers: MessageHandlers, redis: Redis, configs: ConfigRepository
) -> None:
    cfg = await _published(handlers, redis, configs, release_in=timedelta(days=1))
    await deliver(handlers, messages.event_updated(cfg.event_id, version=3, total_capacity=50))

    result = await deliver(
        handlers, messages.event_updated(cfg.event_id, version=2, total_capacity=999)
    )

    assert result.outcome is Outcome.STALE
    stored = await configs.load(cfg.event_id)
    assert stored is not None and (stored.event_version, stored.total_capacity) == (3, 50)


async def test_update_for_unknown_event_is_an_upsert(
    handlers: MessageHandlers, configs: ConfigRepository
) -> None:
    eid = event_id()
    result = await deliver(handlers, messages.event_updated(eid, version=2, total_capacity=20))
    assert result.outcome is Outcome.CREATED
    cfg = await configs.load(eid)
    assert cfg is not None and (cfg.event_version, cfg.status) == (2, QueueStatus.SCHEDULED)

    # The late EventPublished (v1) is then stale.
    late = await deliver(handlers, messages.event_published(eid))
    assert late.outcome is Outcome.STALE


@pytest.mark.parametrize(
    "release_in",
    [timedelta(minutes=10), -timedelta(minutes=1)],
    ids=["open", "admitting"],
)
async def test_update_rejected_once_lobby_is_open(
    handlers: MessageHandlers,
    redis: Redis,
    configs: ConfigRepository,
    caplog: pytest.LogCaptureFixture,
    release_in: timedelta,
) -> None:
    cfg = await _published(handlers, redis, configs, release_in=release_in)

    with caplog.at_level(logging.WARNING):
        result = await deliver(
            handlers, messages.event_updated(cfg.event_id, version=2, total_capacity=999)
        )

    assert result.outcome is Outcome.REJECTED
    assert await configs.load(cfg.event_id) == cfg  # row unchanged, version included
    assert "event_update_rejected" in caplog.text


async def test_update_after_close_is_ignored(
    handlers: MessageHandlers, redis: Redis, configs: ConfigRepository
) -> None:
    cfg = await _published(handlers, redis, configs, release_in=timedelta(days=1))
    await deliver(handlers, messages.event_sold_out(cfg.event_id))

    result = await deliver(handlers, messages.event_updated(cfg.event_id, version=2))

    assert result.outcome is Outcome.IGNORED


# --- EventCancelled ---------------------------------------------------------------------------


async def test_cancel_clears_everything(
    handlers: MessageHandlers,
    redis: Redis,
    configs: ConfigRepository,
    queues: QueueRepository,
) -> None:
    cfg, users, admitted = await _admitting(handlers, redis, configs, queues)
    await queues.add_live(cfg.event_id)
    msg = messages.event_cancelled(cfg.event_id, version=2)

    result = await deliver(handlers, msg)

    assert result.outcome is Outcome.CANCELLED
    stored = await configs.load(cfg.event_id)
    assert stored is not None
    assert (stored.status, stored.event_version) == (QueueStatus.CANCELLED, 2)
    assert stored.closed_at is not None
    eid = cfg.event_id
    assert [k async for k in redis.scan_iter(match=f"queue:{{{eid}}}:*")] == [keys.config(eid)]
    assert await redis.hget(keys.config(eid), "status") == "CANCELLED"
    assert not await redis.sismember(keys.LIVE_EVENTS, eid)
    assert not await redis.hexists(keys.ADMISSION_INDEX, admitted[0].admission_id)
    assert (await queues.join(cfg, users[1])).outcome is JoinOutcome.CLOSED


async def test_duplicate_cancel_reapplies_redis_cleanup(
    handlers: MessageHandlers, redis: Redis, configs: ConfigRepository, queues: QueueRepository
) -> None:
    """E.g. Redis failed after the MySQL commit, and the message was redelivered."""
    cfg, _, _ = await _admitting(handlers, redis, configs, queues)
    msg = messages.event_cancelled(cfg.event_id, version=2)
    await deliver(handlers, msg)
    await redis.zadd(keys.waiting(cfg.event_id), {"leftover": 1})  # simulate missed cleanup

    result = await deliver(handlers, msg)

    assert result.outcome is Outcome.REAPPLIED
    assert not await redis.exists(keys.waiting(cfg.event_id))


async def test_stale_cancel_and_unknown_event(
    handlers: MessageHandlers, redis: Redis, configs: ConfigRepository
) -> None:
    cfg = await _published(handlers, redis, configs, release_in=timedelta(days=1))
    await deliver(handlers, messages.event_updated(cfg.event_id, version=5))

    stale = await deliver(handlers, messages.event_cancelled(cfg.event_id, version=4))
    unknown = await deliver(handlers, messages.event_cancelled(event_id(), version=2))

    assert (stale.outcome, unknown.outcome) == (Outcome.STALE, Outcome.UNKNOWN_EVENT)
    stored = await configs.load(cfg.event_id)
    assert stored is not None and stored.status is QueueStatus.SCHEDULED


async def test_sold_out_event_can_still_be_cancelled(
    handlers: MessageHandlers, redis: Redis, configs: ConfigRepository, queues: QueueRepository
) -> None:
    cfg, _, _ = await _admitting(handlers, redis, configs, queues)
    await deliver(handlers, messages.event_sold_out(cfg.event_id))

    result = await deliver(handlers, messages.event_cancelled(cfg.event_id, version=2))

    assert result.outcome is Outcome.CANCELLED
    assert await queues.pool_size(cfg.event_id) == 0
    assert not await redis.sismember(keys.DRAINING_EVENTS, cfg.event_id)


# --- EventSoldOut -----------------------------------------------------------------------------


async def test_sold_out_keeps_admissions_and_clears_waiting(
    handlers: MessageHandlers, redis: Redis, configs: ConfigRepository, queues: QueueRepository
) -> None:
    cfg, users, admitted = await _admitting(handlers, redis, configs, queues)
    await queues.add_live(cfg.event_id)
    sold_out_at = await server_now(redis)

    result = await deliver(handlers, messages.event_sold_out(cfg.event_id, sold_out_at=sold_out_at))

    assert result.outcome is Outcome.CLOSED
    stored = await configs.load(cfg.event_id)
    assert stored is not None
    assert (stored.status, stored.closed_at) == (QueueStatus.CLOSED, sold_out_at)
    eid = cfg.event_id
    assert not await redis.exists(keys.waiting(eid))
    assert await queues.pool_size(eid) == 1
    assert not await redis.sismember(keys.LIVE_EVENTS, eid)
    assert await redis.sismember(keys.DRAINING_EVENTS, eid)
    assert (await queues.join(cfg, users[1])).outcome is JoinOutcome.CLOSED
    assert (await queues.join(cfg, users[0])).admission_id == admitted[0].admission_id


async def test_sold_out_duplicate_after_cancel_and_unknown(
    handlers: MessageHandlers, redis: Redis, configs: ConfigRepository
) -> None:
    cfg = await _published(handlers, redis, configs, release_in=-timedelta(seconds=1))
    msg = messages.event_sold_out(cfg.event_id)
    assert (await deliver(handlers, msg)).outcome is Outcome.CLOSED
    assert (await deliver(handlers, msg)).outcome is Outcome.REAPPLIED

    await deliver(handlers, messages.event_cancelled(cfg.event_id, version=2))
    late = await deliver(handlers, messages.event_sold_out(cfg.event_id))
    unknown = await deliver(handlers, messages.event_sold_out(event_id()))

    assert (late.outcome, unknown.outcome) == (Outcome.IGNORED, Outcome.UNKNOWN_EVENT)
    stored = await configs.load(cfg.event_id)
    assert stored is not None and stored.status is QueueStatus.CANCELLED


# --- OrderConfirmed --------------------------------------------------------------------------


async def test_order_confirmed_frees_slot_and_admits_next_at_once(
    handlers: MessageHandlers,
    redis: Redis,
    configs: ConfigRepository,
    queues: QueueRepository,
    sessions: Sessions,
) -> None:
    cfg, users, (adm,) = await _admitting(handlers, redis, configs, queues)
    msg = messages.order_confirmed(
        admission_id=adm.admission_id, user_id=adm.user_id, event_id=cfg.event_id
    )

    result = await deliver(handlers, msg)

    assert (result.outcome, result.admitted) == (Outcome.RELEASED, 1)
    now = await server_now(redis)
    assert (await queues.get_entry(cfg.event_id, users[0], now)).status.value == "NONE"
    assert (await queues.get_entry(cfg.event_id, users[1], now)).status.value == "ADMITTED"

    again = await deliver(handlers, msg)  # redelivery: no second admission
    assert (again.outcome, again.admitted) == (Outcome.NOT_FOUND, 0)


async def test_order_confirmed_mismatch_changes_nothing(
    handlers: MessageHandlers,
    redis: Redis,
    configs: ConfigRepository,
    queues: QueueRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg, _, (adm,) = await _admitting(handlers, redis, configs, queues)

    with caplog.at_level(logging.ERROR):
        wrong_user = await deliver(
            handlers,
            messages.order_confirmed(
                admission_id=adm.admission_id, user_id=user_id(), event_id=cfg.event_id
            ),
        )
        wrong_event = await deliver(
            handlers,
            messages.order_confirmed(
                admission_id=adm.admission_id, user_id=adm.user_id, event_id=event_id()
            ),
        )

    assert (wrong_user.outcome, wrong_event.outcome) == (Outcome.MISMATCH, Outcome.MISMATCH)
    assert await queues.pool_size(cfg.event_id) == 1
    assert "admission_integrity_mismatch" in caplog.text


async def test_order_confirmed_for_unknown_or_expired_admission(
    handlers: MessageHandlers, redis: Redis, configs: ConfigRepository, queues: QueueRepository
) -> None:
    cfg, _, (adm,) = await _admitting(handlers, redis, configs, queues)
    unknown = await deliver(
        handlers,
        messages.order_confirmed(admission_id=user_id(), user_id=user_id(), event_id=cfg.event_id),
    )
    await asyncio.sleep(2.2)  # past the 2 s test deadline, not yet swept
    expired = await deliver(
        handlers,
        messages.order_confirmed(
            admission_id=adm.admission_id, user_id=adm.user_id, event_id=cfg.event_id
        ),
    )

    assert (unknown.outcome, expired.outcome) == (Outcome.NOT_FOUND, Outcome.EXPIRED)
    assert expired.admitted == 0


async def test_order_confirmed_after_sell_out_admits_nobody(
    handlers: MessageHandlers, redis: Redis, configs: ConfigRepository, queues: QueueRepository
) -> None:
    cfg, _, (adm,) = await _admitting(handlers, redis, configs, queues, users=3)
    await deliver(handlers, messages.event_sold_out(cfg.event_id))

    result = await deliver(
        handlers,
        messages.order_confirmed(
            admission_id=adm.admission_id, user_id=adm.user_id, event_id=cfg.event_id
        ),
    )

    assert (result.outcome, result.admitted) == (Outcome.RELEASED, 0)
    assert await queues.pool_size(cfg.event_id) == 0
