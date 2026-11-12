from __future__ import annotations

from random import Random
from typing import cast

import pytest
from redis.asyncio import Redis

from app.queue import keys
from app.queue.repository import QueueRepository, _safety_expiry_ms
from tests.fixtures.factories import user_id
from tests.integration.redis.conftest import MakeEvent, waiting_order

pytestmark = pytest.mark.integration


async def test_shuffle_orders_prequeue_with_given_rng(
    redis: Redis, repo: QueueRepository, open_event: MakeEvent
) -> None:
    cfg = await open_event()
    users = [user_id() for _ in range(20)]
    for user in users:
        await repo.join(cfg, user)

    result = await repo.shuffle(cfg, Random(42))

    expected = sorted(users)
    Random(42).shuffle(expected)
    assert (result.performed, result.ordered, result.leftovers) == (True, 20, 0)
    assert await waiting_order(redis, cfg) == expected
    assert not await redis.exists(keys.prequeue(cfg.event_id))
    assert await redis.get(keys.shuffled(cfg.event_id)) == "1"


async def test_every_member_gets_exactly_one_contiguous_seq(
    redis: Redis, repo: QueueRepository, open_event: MakeEvent
) -> None:
    cfg = await open_event()
    for _ in range(50):
        await repo.join(cfg, user_id())

    await repo.shuffle(cfg)  # real SystemRandom

    rows = cast(
        list[tuple[str, float]],
        await redis.zrange(keys.waiting(cfg.event_id), 0, -1, withscores=True),
    )
    scores = [int(score) for _, score in rows]
    assert scores == list(range(1, 51))


async def test_shuffle_runs_exactly_once(
    redis: Redis, repo: QueueRepository, open_event: MakeEvent
) -> None:
    cfg = await open_event()
    for _ in range(5):
        await repo.join(cfg, user_id())
    await repo.shuffle(cfg, Random(1))
    order = await waiting_order(redis, cfg)

    late = user_id()
    await repo.join(cfg, late)  # after shuffle: appended FIFO
    again = await repo.shuffle(cfg, Random(2))

    assert not again.performed
    assert await waiting_order(redis, cfg) == [*order, late]


async def test_joins_during_shuffle_are_appended_not_lost(
    redis: Redis, repo: QueueRepository, open_event: MakeEvent
) -> None:
    """Python read the pre-queue as [a, b]; c joined before the script ran."""
    cfg = await open_event()
    a, b, c = user_id(), user_id(), user_id()
    for user in (a, b, c):
        await repo.join(cfg, user)
    eid = cfg.event_id

    # Call the script directly with the stale snapshot, as repo.shuffle would.
    reply = await repo._shuffle(
        keys=[keys.shuffled(eid), keys.prequeue(eid), keys.waiting(eid), keys.seq(eid)],
        args=[_safety_expiry_ms(cfg), b, a],
    )

    assert [int(x) for x in reply] == [1, 2, 1]
    assert await waiting_order(redis, cfg) == [b, a, c]


async def test_duplicates_and_strangers_in_order_are_ignored(
    redis: Redis, repo: QueueRepository, open_event: MakeEvent
) -> None:
    cfg = await open_event()
    a, b = user_id(), user_id()
    for user in (a, b):
        await repo.join(cfg, user)
    eid = cfg.event_id

    reply = await repo._shuffle(
        keys=[keys.shuffled(eid), keys.prequeue(eid), keys.waiting(eid), keys.seq(eid)],
        args=[_safety_expiry_ms(cfg), a, a, user_id(), b],
    )

    assert [int(x) for x in reply] == [1, 2, 0]
    assert await waiting_order(redis, cfg) == [a, b]


async def test_empty_prequeue_still_marks_event_shuffled(
    redis: Redis, repo: QueueRepository, open_event: MakeEvent
) -> None:
    cfg = await open_event()

    result = await repo.shuffle(cfg)

    assert (result.performed, result.ordered, result.leftovers) == (True, 0, 0)
    assert await repo.is_shuffled(cfg.event_id)
    first = user_id()
    await repo.join(cfg, first)
    assert await waiting_order(redis, cfg) == [first]
