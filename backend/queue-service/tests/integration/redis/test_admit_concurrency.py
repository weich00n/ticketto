"""The pool never exceeds capacity, and nobody is admitted twice or out of
order, while admitter ticks and OrderConfirmed releases race each other.

Set CONCURRENCY_REPEATS (default 1) to repeat the scenario, e.g. 20 in CI once.
"""

from __future__ import annotations

import asyncio
import os
import random

import pytest
from redis.asyncio import Redis

from app.queue import keys
from app.queue.models import Admission, AdmitResult, ReleaseResult
from app.queue.repository import QueueRepository
from tests.fixtures.factories import user_id
from tests.integration.redis.conftest import MakeEvent

pytestmark = pytest.mark.integration

CAPACITY = 5
WAITING = 1_000
PARALLEL_ADMITS = 50
REPEATS = int(os.environ.get("CONCURRENCY_REPEATS", "1"))


@pytest.mark.parametrize("repeat", range(REPEATS))
async def test_pool_never_overfills_under_racing_admits_and_releases(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent, repeat: int
) -> None:
    users = [user_id() for _ in range(WAITING)]
    cfg = await admitting_event(capacity=CAPACITY)
    seq_of: dict[str, int] = {}
    async with redis.pipeline(transaction=False) as pipe:
        for position, user in enumerate(users, start=1):
            pipe.zadd(keys.waiting(cfg.event_id), {user: position})
            seq_of[user] = position
        await pipe.execute()
    await redis.set(keys.seq(cfg.event_id), WAITING)

    rng = random.Random(repeat)
    ever_admitted: list[Admission] = []
    active: dict[str, Admission] = {}
    peak = 0
    stop = asyncio.Event()

    async def monitor() -> None:
        nonlocal peak
        while not stop.is_set():
            peak = max(peak, await redis.zcard(keys.active(cfg.event_id)))
            await asyncio.sleep(0)

    monitor_task = asyncio.create_task(monitor())
    try:
        while len(ever_admitted) < WAITING:
            # Release a random subset of the current pool while 50 admits race.
            to_release = rng.sample(list(active.values()), k=rng.randint(0, len(active)))
            calls: list[asyncio.Future[AdmitResult | ReleaseResult]] = [
                asyncio.ensure_future(repo.admit(cfg)) for _ in range(PARALLEL_ADMITS)
            ]
            calls += [
                asyncio.ensure_future(repo.release(a.admission_id, a.user_id, cfg.event_id))
                for a in to_release
            ]
            rng.shuffle(calls)
            results = await asyncio.gather(*calls)

            for released in to_release:
                active.pop(released.admission_id, None)
            for result in results:
                if isinstance(result, AdmitResult):
                    for adm in result.admitted:
                        ever_admitted.append(adm)
                        active[adm.admission_id] = adm

            pool = await redis.zcard(keys.active(cfg.event_id))
            assert pool <= CAPACITY
            assert pool == len(active)
            # FIFO: everyone still waiting is behind everyone ever admitted.
            remaining = await redis.zrange(keys.waiting(cfg.event_id), 0, 0, withscores=True)
            if remaining and ever_admitted:
                assert int(remaining[0][1]) > max(seq_of[a.user_id] for a in ever_admitted)
            if not to_release and pool == CAPACITY:
                # Pool full and nothing released this round: release everything next.
                for adm in list(active.values()):
                    await repo.release(adm.admission_id, adm.user_id, cfg.event_id)
                    active.pop(adm.admission_id)
    finally:
        stop.set()
        await monitor_task

    assert peak <= CAPACITY
    admitted_users = [a.user_id for a in ever_admitted]
    assert len(set(admitted_users)) == len(admitted_users) == WAITING
    # Global FIFO: scripts run one at a time, so in queue order the admission
    # times never go backwards.
    by_seq = sorted(ever_admitted, key=lambda a: seq_of[a.user_id])
    assert [a.user_id for a in by_seq] == users
    times = [a.admitted_at for a in by_seq]
    assert times == sorted(times)
