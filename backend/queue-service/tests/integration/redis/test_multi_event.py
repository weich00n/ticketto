"""Two events admitting at the same time stay fully independent, even for a
buyer who is in both queues."""

from __future__ import annotations

import pytest
from redis.asyncio import Redis

from app.core.redis import server_now
from app.core.status import EntryStatus, QueueStatus
from app.queue import keys
from app.queue.models import JoinOutcome, ReleaseOutcome
from app.queue.repository import QueueRepository
from tests.fixtures.factories import user_id
from tests.integration.redis.conftest import MakeEvent, set_cached_status, waiting_order

pytestmark = pytest.mark.integration


async def test_same_buyer_admitted_in_two_events(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    shared = user_id()
    first = await admitting_event(capacity=1, users=[shared, user_id()])
    second = await admitting_event(capacity=2, users=[user_id(), shared])

    (adm_first,) = (await repo.admit(first)).admitted
    admitted_second = (await repo.admit(second)).admitted

    assert adm_first.user_id == shared and adm_first.event_id == first.event_id
    assert shared in {a.user_id for a in admitted_second}
    assert {a.event_id for a in admitted_second} == {second.event_id}

    now = await server_now(redis)
    entry_first = await repo.get_entry(first.event_id, shared, now)
    entry_second = await repo.get_entry(second.event_id, shared, now)
    assert entry_first.admission is not None and entry_second.admission is not None
    assert entry_first.admission.admission_id != entry_second.admission.admission_id


async def test_release_in_one_event_does_not_touch_the_other(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    first = await admitting_event(capacity=1, users=[user_id(), user_id()])
    second = await admitting_event(capacity=1, users=[user_id(), user_id()])
    (adm_first,) = (await repo.admit(first)).admitted
    (adm_second,) = (await repo.admit(second)).admitted

    # Releasing first's admission while claiming it belongs to second is refused.
    wrong = await repo.release(adm_first.admission_id, adm_first.user_id, second.event_id)
    assert wrong.outcome is ReleaseOutcome.MISMATCH

    await repo.release(adm_first.admission_id, adm_first.user_id, first.event_id)
    assert len((await repo.admit(first)).admitted) == 1
    assert (await repo.admit(second)).admitted == []
    assert await redis.zscore(keys.active(second.event_id), adm_second.admission_id) is not None


async def test_closing_one_event_leaves_the_other_open(
    redis: Redis, repo: QueueRepository, admitting_event: MakeEvent
) -> None:
    shared = user_id()
    first = await admitting_event(capacity=1, users=[user_id(), shared])
    second = await admitting_event(capacity=1, users=[user_id(), shared])
    await set_cached_status(redis, first, QueueStatus.CANCELLED)

    await repo.cancel_event(first.event_id)

    now = await server_now(redis)
    assert (await repo.get_entry(first.event_id, shared, now)).status is EntryStatus.NONE
    second_entry = await repo.get_entry(second.event_id, shared, now)
    assert (second_entry.status, second_entry.position) == (EntryStatus.WAITING, 2)
    assert (await repo.join(first, user_id())).outcome is JoinOutcome.CLOSED
    assert (await repo.join(second, user_id())).outcome is JoinOutcome.CREATED
    assert len(await waiting_order(redis, second)) == 3
