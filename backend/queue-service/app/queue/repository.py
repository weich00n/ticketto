"""Redis queue state: Lua-script wrappers, reads and per-event cleanup.

Every mutation of an event's queue goes through one of the Lua scripts in
app/queue/lua, so it is atomic. Only global keys (live/draining sets, the
admission index) are touched outside scripts, which keeps the scripts
single-slot and Redis Cluster-safe.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import resources
from random import Random
from typing import Any, cast

from redis.asyncio import Redis
from redis.commands.core import AsyncScript

from app.core.config import ADMISSION_WINDOW_SECONDS, SELECTION_WINDOW_SECONDS
from app.core.cursor import InvalidCursor, decode_cursor, encode_cursor
from app.core.status import EntryStatus
from app.core.timefmt import from_ms, to_ms
from app.queue import keys
from app.queue.config_repo import SAFETY_TTL_AFTER_RELEASE, QueueConfigData
from app.queue.models import (
    Admission,
    AdmissionPage,
    AdmitResult,
    Entry,
    JoinOutcome,
    JoinResult,
    LapseOutcome,
    LapseResult,
    LeaveOutcome,
    QueueCounts,
    ReleasedAdmission,
    ReleaseOutcome,
    ReleaseResult,
    ShuffleResult,
)

# After a sell-out, the remaining pool keys live this long past the last deadline.
DRAIN_GRACE = timedelta(hours=1)
MAX_PAGE_SIZE = 200


def _load_script(redis: Redis, name: str) -> AsyncScript:
    source = resources.files("app.queue.lua").joinpath(f"{name}.lua").read_text()
    return redis.register_script(source)


def _text(value: bytes | str | None) -> str | None:
    """redis-py types replies as bytes | str; the client decodes, so expect str."""
    if isinstance(value, bytes):
        return value.decode()
    return value


def _safety_expiry_ms(cfg: QueueConfigData) -> int:
    return to_ms(cfg.release_at + SAFETY_TTL_AFTER_RELEASE)


@dataclass(frozen=True, slots=True)
class _AdmissionsCursor:
    score: int
    admission_id: str


class QueueRepository:
    def __init__(
        self,
        redis: Redis,
        *,
        selection_window_seconds: int = SELECTION_WINDOW_SECONDS,
        admission_window_seconds: int = ADMISSION_WINDOW_SECONDS,
        new_admission_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ):
        self._redis = redis
        self._selection_ms = selection_window_seconds * 1000
        self._admission_ms = admission_window_seconds * 1000
        self._new_admission_id = new_admission_id
        self._join = _load_script(redis, "join")
        self._shuffle = _load_script(redis, "shuffle")
        self._admit = _load_script(redis, "admit")
        self._release = _load_script(redis, "release")
        self._leave = _load_script(redis, "leave")
        self._requeue_lapsed = _load_script(redis, "requeue_lapsed")

    # --- scripts ---------------------------------------------------------------

    async def join(self, cfg: QueueConfigData, user_id: str) -> JoinResult:
        """Join, or return existing state. Status checks for SCHEDULED belong to
        the caller; CLOSED/CANCELLED are also enforced here, atomically."""
        eid = cfg.event_id
        reply = await self._join(
            keys=[
                keys.config(eid),
                keys.user_admission(eid),
                keys.prequeue(eid),
                keys.waiting(eid),
                keys.seq(eid),
                keys.shuffled(eid),
                keys.active(eid),
            ],
            args=[user_id, _safety_expiry_ms(cfg), keys.admission_prefix(eid)],
        )
        outcome, detail, swept = (str(part) for part in reply)
        if swept:
            await self._redis.hdel(keys.ADMISSION_INDEX, swept)
        match JoinOutcome(outcome):
            case JoinOutcome.ALREADY_ADMITTED:
                return JoinResult(JoinOutcome.ALREADY_ADMITTED, admission_id=detail)
            case JoinOutcome.CLOSED:
                return JoinResult(JoinOutcome.CLOSED, closed_status=detail)
            case other:
                return JoinResult(other)

    async def shuffle(self, cfg: QueueConfigData, rng: Random | None = None) -> ShuffleResult:
        """Shuffle the pre-queue into FIFO order. Runs at most once per event."""
        eid = cfg.event_id
        members = [str(_text(m)) for m in await self._redis.smembers(keys.prequeue(eid))]
        members.sort()  # deterministic input, so a seeded rng gives a repeatable order
        (rng or secrets.SystemRandom()).shuffle(members)
        reply = await self._shuffle(
            keys=[keys.shuffled(eid), keys.prequeue(eid), keys.waiting(eid), keys.seq(eid)],
            args=[_safety_expiry_ms(cfg), *members],
        )
        performed, ordered, leftovers = (int(part) for part in reply)
        if not performed:
            return ShuffleResult(performed=False)
        return ShuffleResult(performed=True, ordered=ordered, leftovers=leftovers)

    async def admit(self, cfg: QueueConfigData) -> AdmitResult:
        """Sweep expired admissions and fill free slots (only once ADMITTING)."""
        eid = cfg.event_id
        capacity = cfg.active_pool_capacity
        admission_ids = [self._new_admission_id() for _ in range(capacity)]
        reply = await self._admit(
            keys=[
                keys.config(eid),
                keys.active(eid),
                keys.user_admission(eid),
                keys.waiting(eid),
                keys.shuffled(eid),
            ],
            args=[
                keys.admission_prefix(eid),
                eid,
                capacity,
                self._selection_ms,
                self._admission_ms,
                _safety_expiry_ms(cfg),
                *admission_ids,
            ],
        )
        released_flat, admitted_flat, now_ms = reply
        released = [
            ReleasedAdmission(str(released_flat[i]), str(released_flat[i + 1]) or None)
            for i in range(0, len(released_flat), 2)
        ]
        admitted = [
            Admission(
                admission_id=str(admitted_flat[i]),
                user_id=str(admitted_flat[i + 1]),
                event_id=eid,
                admitted_at=from_ms(int(admitted_flat[i + 2])),
                selection_expires_at=from_ms(int(admitted_flat[i + 3])),
                admission_expires_at=from_ms(int(admitted_flat[i + 4])),
            )
            for i in range(0, len(admitted_flat), 5)
        ]
        await self._update_index(eid, admitted, released)
        return AdmitResult(admitted=admitted, released=released, now=from_ms(int(now_ms)))

    async def release(self, admission_id: str, user_id: str, event_id: str) -> ReleaseResult:
        """End an admission after OrderConfirmed. Never changes anything unless
        the admission exists, is unexpired, and matches user_id and event_id."""
        indexed_event = _text(await self._redis.hget(keys.ADMISSION_INDEX, admission_id))
        if indexed_event is not None and indexed_event != event_id:
            # The admission lives under another event: the message is wrong.
            stored = _text(
                await self._redis.hget(keys.admission(indexed_event, admission_id), "user_id")
            )
            if stored is not None:
                return ReleaseResult(ReleaseOutcome.MISMATCH, stored, indexed_event)
        reply = await self._release(
            keys=[
                keys.active(event_id),
                keys.user_admission(event_id),
                keys.admission(event_id, admission_id),
            ],
            args=[admission_id, user_id, event_id],
        )
        outcome, stored_user, stored_event = (str(part) for part in reply)
        result = ReleaseResult(ReleaseOutcome(outcome), stored_user or None, stored_event or None)
        if result.outcome in (ReleaseOutcome.RELEASED, ReleaseOutcome.NOT_FOUND):
            await self._redis.hdel(keys.ADMISSION_INDEX, admission_id)
        return result

    async def requeue_lapsed(self, cfg: QueueConfigData, user_id: str) -> LapseResult:
        """End the buyer's admission if its selection deadline has passed, and put
        them at the back of the queue (not requeued if the queue is closed). The
        caller admits the next buyer into the freed slot."""
        eid = cfg.event_id
        reply = await self._requeue_lapsed(
            keys=[
                keys.config(eid),
                keys.user_admission(eid),
                keys.active(eid),
                keys.waiting(eid),
                keys.seq(eid),
                keys.shuffled(eid),
            ],
            args=[user_id, keys.admission_prefix(eid), _safety_expiry_ms(cfg)],
        )
        outcome, admission_id = (str(part) for part in reply)
        result = LapseResult(LapseOutcome(outcome), admission_id or None)
        if result.ended:
            await self._redis.hdel(keys.ADMISSION_INDEX, admission_id)
        return result

    async def leave(self, event_id: str, user_id: str) -> LeaveOutcome:
        reply = await self._leave(
            keys=[
                keys.user_admission(event_id),
                keys.waiting(event_id),
                keys.prequeue(event_id),
            ],
            args=[user_id, keys.admission_prefix(event_id)],
        )
        return LeaveOutcome(str(reply))

    # --- event-level cleanup -------------------------------------------------------
    #
    # Callers must write the terminal status to the config cache first
    # (ConfigRepository.set_status does), so the join and admit scripts already
    # refuse new work while these run.

    async def cancel_event(self, event_id: str) -> list[ReleasedAdmission]:
        """EventCancelled: delete every queue, pool and admission key for the event.

        Returns the admissions that were ended.
        """
        admission_ids = cast(list[str], await self._redis.zrange(keys.active(event_id), 0, -1))
        released: list[ReleasedAdmission] = []
        if admission_ids:
            async with self._redis.pipeline(transaction=False) as pipe:
                for admission_id in admission_ids:
                    pipe.hget(keys.admission(event_id, admission_id), "user_id")
                user_ids = await pipe.execute()
            released = [
                ReleasedAdmission(aid, uid)
                for aid, uid in zip(admission_ids, user_ids, strict=True)
            ]
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.delete(
                keys.prequeue(event_id),
                keys.waiting(event_id),
                keys.seq(event_id),
                keys.shuffled(event_id),
                keys.active(event_id),
                keys.user_admission(event_id),
                *(keys.admission(event_id, aid) for aid in admission_ids),
            )
            if admission_ids:
                pipe.hdel(keys.ADMISSION_INDEX, *admission_ids)
            pipe.srem(keys.LIVE_EVENTS, event_id)
            pipe.srem(keys.DRAINING_EVENTS, event_id)
            await pipe.execute()
        return released

    async def close_event(self, event_id: str) -> bool:
        """EventSoldOut: clear the waiting queue but keep the active pool.

        The pool's keys expire an hour after its last deadline, and the event moves
        to the draining set so the admitter keeps sweeping it. Returns True when
        admissions remain (the event is draining).
        """
        pool = cast(
            list[tuple[str, float]],
            await self._redis.zrange(keys.active(event_id), 0, -1, withscores=True),
        )
        grace_ms = int(DRAIN_GRACE.total_seconds() * 1000)
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.delete(keys.prequeue(event_id), keys.waiting(event_id))
            pipe.srem(keys.LIVE_EVENTS, event_id)
            if pool:
                last_deadline = int(max(score for _, score in pool))
                for admission_id, score in pool:
                    pipe.pexpireat(keys.admission(event_id, admission_id), int(score) + grace_ms)
                for key in (
                    keys.active(event_id),
                    keys.user_admission(event_id),
                    keys.shuffled(event_id),
                    keys.seq(event_id),
                ):
                    pipe.pexpireat(key, last_deadline + grace_ms)
                pipe.sadd(keys.DRAINING_EVENTS, event_id)
            await pipe.execute()
        return bool(pool)

    # --- reads -------------------------------------------------------------------

    async def get_entry(self, event_id: str, user_id: str, now: datetime) -> Entry:
        """Lookup order: admitted, then waiting (with position), then pre-queue.

        An admission past its deadline counts as ended even before the sweep.
        """
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.hget(keys.user_admission(event_id), user_id)
            pipe.zrank(keys.waiting(event_id), user_id)
            pipe.sismember(keys.prequeue(event_id), user_id)
            admission_id, rank, in_prequeue = await pipe.execute()
        if admission_id is not None:
            admission = await self._read_admission(event_id, admission_id)
            if admission is not None and not admission.is_expired(now):
                return Entry(EntryStatus.ADMITTED, admission=admission)
        if rank is not None:
            return Entry(EntryStatus.WAITING, position=int(rank) + 1)
        if in_prequeue:
            return Entry(EntryStatus.WAITING, position=None)
        return Entry(EntryStatus.NONE)

    async def counts(self, event_id: str, now: datetime) -> QueueCounts:
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.zcard(keys.waiting(event_id))
            pipe.scard(keys.prequeue(event_id))
            pipe.zcount(keys.active(event_id), f"({to_ms(now)}", "+inf")
            waiting, prequeue, active = await pipe.execute()
        return QueueCounts(waiting=int(waiting) + int(prequeue), active=int(active))

    async def is_shuffled(self, event_id: str) -> bool:
        return bool(await self._redis.exists(keys.shuffled(event_id)))

    async def pool_size(self, event_id: str) -> int:
        """Admissions in the pool, including expired ones not swept yet."""
        return int(await self._redis.zcard(keys.active(event_id)))

    # --- admitter work lists (global keys) -------------------------------------------

    async def live_event_ids(self) -> list[str]:
        return sorted(str(_text(e)) for e in await self._redis.smembers(keys.LIVE_EVENTS))

    async def add_live(self, *event_ids: str) -> None:
        if event_ids:
            await self._redis.sadd(keys.LIVE_EVENTS, *event_ids)

    async def remove_live(self, event_id: str) -> None:
        await self._redis.srem(keys.LIVE_EVENTS, event_id)

    async def draining_event_ids(self) -> list[str]:
        return sorted(str(_text(e)) for e in await self._redis.smembers(keys.DRAINING_EVENTS))

    async def remove_draining(self, event_id: str) -> None:
        await self._redis.srem(keys.DRAINING_EVENTS, event_id)

    async def get_admission(self, admission_id: str, now: datetime) -> Admission | None:
        """Look up an admission by id (internal API). None once it has ended."""
        event_id = _text(await self._redis.hget(keys.ADMISSION_INDEX, admission_id))
        if event_id is None:
            return None
        admission = await self._read_admission(event_id, admission_id)
        if admission is None:
            await self._redis.hdel(keys.ADMISSION_INDEX, admission_id)  # lazily drop stale entry
            return None
        return None if admission.is_expired(now) else admission

    async def list_admissions(
        self, event_id: str, now: datetime, limit: int, cursor: str | None
    ) -> AdmissionPage:
        """Unexpired admissions, oldest first, keyset-paginated.

        Order is (admission_expires_at, admission_id). Every admission lasts the
        same window, so that is also admitted_at order. Admissions made in one
        admit call share a score; ties are resolved by id.

        Raises InvalidCursor for a malformed cursor.
        """
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        after = _parse_admissions_cursor(cursor) if cursor is not None else None
        low = to_ms(now) + 1  # deadline <= now means expired
        if after is not None:
            low = max(low, after.score)

        picked: list[tuple[str, int]] = []
        batch = max(limit + 1, 200)
        offset = 0
        while len(picked) <= limit:
            rows = cast(
                list[tuple[str, float]],
                await self._redis.zrangebyscore(
                    keys.active(event_id), low, "+inf", start=offset, num=batch, withscores=True
                ),
            )
            for admission_id, raw_score in rows:
                score = int(raw_score)
                if after is not None and (score, admission_id) <= (after.score, after.admission_id):
                    continue
                picked.append((admission_id, score))
                if len(picked) > limit:
                    break
            if len(rows) < batch:
                break
            offset += batch

        page = picked[:limit]
        items = [
            admission
            for admission in await self._read_admissions(event_id, [aid for aid, _ in page])
            if admission is not None
        ]
        next_cursor = None
        if len(picked) > limit:
            last_id, last_score = page[-1]
            next_cursor = encode_cursor({"s": last_score, "a": last_id})
        return AdmissionPage(items=items, next_cursor=next_cursor)

    # --- helpers -----------------------------------------------------------------

    async def _read_admission(self, event_id: str, admission_id: str) -> Admission | None:
        (admission,) = await self._read_admissions(event_id, [admission_id])
        return admission

    async def _read_admissions(
        self, event_id: str, admission_ids: Sequence[str]
    ) -> list[Admission | None]:
        if not admission_ids:
            return []
        async with self._redis.pipeline(transaction=False) as pipe:
            for admission_id in admission_ids:
                pipe.hgetall(keys.admission(event_id, admission_id))
            raws: list[dict[str, str]] = await pipe.execute()
        return [Admission.from_hash(raw) if raw else None for raw in raws]

    async def _update_index(
        self, event_id: str, admitted: list[Admission], released: list[ReleasedAdmission]
    ) -> None:
        if not admitted and not released:
            return
        async with self._redis.pipeline(transaction=False) as pipe:
            if admitted:
                pipe.hset(
                    keys.ADMISSION_INDEX,
                    mapping={a.admission_id: event_id for a in admitted},
                )
            if released:
                pipe.hdel(keys.ADMISSION_INDEX, *(r.admission_id for r in released))
            await pipe.execute()


def _parse_admissions_cursor(cursor: str) -> _AdmissionsCursor:
    data: dict[str, Any] = decode_cursor(cursor)
    score, admission_id = data.get("s"), data.get("a")
    if not isinstance(score, int) or not isinstance(admission_id, str):
        raise InvalidCursor("malformed cursor")
    return _AdmissionsCursor(score=score, admission_id=admission_id)
