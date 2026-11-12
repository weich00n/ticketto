"""Buyer-facing queue operations: lobby info, join, poll and leave.

Combines the config (MySQL + cache), the Redis queue scripts and token signing.
Raises the API's error types; the routes only translate results to bodies.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime

from redis.asyncio import Redis

from app.api.errors import (
    AlreadyAdmitted,
    EventCancelled,
    EventNotFound,
    EventSoldOut,
    NotInQueue,
    QueueNotOpen,
    SaleEnded,
)
from app.core.redis import server_now
from app.core.status import (
    TERMINAL_STATUSES,
    CloseReason,
    EntryStatus,
    QueueStatus,
    RandInt,
    close_reason,
    effective_status,
    poll_after_seconds,
)
from app.core.tokens import AdmissionTokens
from app.queue.config_repo import ConfigRepository, QueueConfigData
from app.queue.models import Entry, JoinOutcome, LeaveOutcome
from app.queue.repository import QueueRepository

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LobbyView:
    cfg: QueueConfigData
    queue_status: QueueStatus
    server_time: datetime


@dataclass(frozen=True, slots=True)
class EntryView:
    cfg: QueueConfigData
    queue_status: QueueStatus
    entry: Entry
    admission_token: str | None
    poll_after_seconds: int | None


class QueueService:
    def __init__(
        self,
        configs: ConfigRepository,
        queues: QueueRepository,
        tokens: AdmissionTokens,
        redis: Redis,
        rng: RandInt | None = None,
    ):
        self._configs = configs
        self._queues = queues
        self._tokens = tokens
        self._redis = redis
        self._rng: RandInt = rng or random.Random()  # poll jitter only, not security

    async def lobby(self, event_id: str) -> LobbyView:
        cfg, now, status = await self._load(event_id)
        return LobbyView(cfg=cfg, queue_status=status, server_time=now)

    async def status(self, event_id: str, user_id: str) -> EntryView:
        cfg, now, status = await self._load(event_id)
        entry = await self._entry(cfg, status, user_id, now)
        return self._view(cfg, status, entry, now)

    async def join(self, event_id: str, user_id: str) -> tuple[bool, EntryView]:
        """Join the queue. Returns (created, view): created is False when the buyer
        was already waiting or admitted (HTTP 200 instead of 201)."""
        cfg, now, status = await self._load(event_id)
        if status is QueueStatus.SCHEDULED:
            raise QueueNotOpen(cfg.queue_opens_at)
        if status in TERMINAL_STATUSES:
            # Closed queue: read-only. A buyer still holding an admission keeps it;
            # nobody else gets in. Nothing is written, even if the sale ended
            # moments ago and the admitter hasn't persisted CLOSED yet.
            entry = await self._entry(cfg, status, user_id, now)
            if entry.status is EntryStatus.ADMITTED:
                return False, self._view(cfg, status, entry, now)
            raise _gone(cfg, status, now)

        result = await self._queues.join(cfg, user_id)
        if result.outcome is JoinOutcome.CLOSED:
            # Sold out or cancelled after we read the config (the script checks
            # the cached status atomically): same answer as if we'd seen it.
            raise _gone(cfg, QueueStatus(result.closed_status or QueueStatus.CLOSED), now)

        entry = await self._entry(cfg, status, user_id, now)
        return result.outcome is JoinOutcome.CREATED, self._view(cfg, status, entry, now)

    async def leave(self, event_id: str, user_id: str) -> None:
        await self._load(event_id)  # 404 EVENT_NOT_FOUND for unknown events
        match await self._queues.leave(event_id, user_id):
            case LeaveOutcome.ALREADY_ADMITTED:
                raise AlreadyAdmitted()
            case LeaveOutcome.NOT_IN_QUEUE:
                raise NotInQueue()
            case LeaveOutcome.LEFT:
                return

    async def _entry(
        self, cfg: QueueConfigData, status: QueueStatus, user_id: str, now: datetime
    ) -> Entry:
        """The buyer's entry. An admitted buyer asking again after their selection
        deadline loses the admission and goes to the back of the queue (not if the
        queue is closed), and the freed slot admits the next buyer at once."""
        entry = await self._queues.get_entry(cfg.event_id, user_id, now)
        if entry.admission is None or now <= entry.admission.selection_expires_at:
            return entry
        lapse = await self._queues.requeue_lapsed(cfg, user_id)
        if not lapse.ended:
            return entry  # the script, on Redis TIME, has the final say
        log.info(
            "admission lapsed",
            extra={
                "event_id": cfg.event_id,
                "user_id": user_id,
                "admission_id": lapse.admission_id,
                "outcome": lapse.outcome.value,
            },
        )
        if status is QueueStatus.ADMITTING:
            await self._queues.admit(cfg)
        return await self._queues.get_entry(cfg.event_id, user_id, now)

    async def _load(self, event_id: str) -> tuple[QueueConfigData, datetime, QueueStatus]:
        now = await server_now(self._redis)
        cfg = await self._configs.get(event_id)
        if cfg is None:
            raise EventNotFound()
        return cfg, now, effective_status(cfg, now)

    def _view(
        self, cfg: QueueConfigData, status: QueueStatus, entry: Entry, now: datetime
    ) -> EntryView:
        if status in TERMINAL_STATUSES and entry.status is EntryStatus.WAITING:
            # Waiting entries end with the queue; cleanup may simply not have run yet.
            entry = Entry(EntryStatus.NONE)
        token = self._tokens.sign(entry.admission) if entry.admission is not None else None
        poll = poll_after_seconds(
            queue_status=status,
            entry_status=entry.status,
            position=entry.position,
            capacity=cfg.active_pool_capacity,
            now=now,
            release_at=cfg.release_at,
            rng=self._rng,
        )
        return EntryView(
            cfg=cfg,
            queue_status=status,
            entry=entry,
            admission_token=token,
            poll_after_seconds=poll,
        )


def _gone(
    cfg: QueueConfigData, status: QueueStatus, now: datetime
) -> EventSoldOut | SaleEnded | EventCancelled:
    if status is QueueStatus.CANCELLED:
        return EventCancelled()
    if close_reason(cfg.sale_ends_at, cfg.closed_at, now) is CloseReason.SALE_ENDED:
        return SaleEnded()
    return EventSoldOut()
