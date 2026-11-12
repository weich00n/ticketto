"""What Queue does with each consumed message. No pika here: the consumer calls
`MessageHandlers.handle` and acks only after it returns.

Idempotency (at-least-once delivery):
- Every handler is safe to run twice. MySQL changes are guarded by event_version
  and forward-only status rules, under a row lock (SELECT ... FOR UPDATE).
- Redis side effects come after the MySQL commit. If they failed, the redelivered
  message must still re-apply them, so a duplicate re-runs the (idempotent) Redis
  part: cache refresh, cancel/close cleanup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import QueueConfig
from app.core.redis import server_now
from app.core.status import (
    TERMINAL_STATUSES,
    QueueStatus,
    active_pool_capacity,
    effective_status,
)
from app.messaging.schemas import (
    EventCancelled,
    EventPublished,
    EventSoldOut,
    EventUpdated,
    Message,
    OrderConfirmed,
)
from app.queue.config_repo import ConfigRepository, QueueConfigData
from app.queue.models import ReleaseOutcome
from app.queue.repository import QueueRepository

log = logging.getLogger(__name__)


class Outcome(StrEnum):
    CREATED = "created"  # EventPublished (or EventUpdated for an unknown event)
    UPDATED = "updated"  # EventUpdated applied while SCHEDULED
    REJECTED = "rejected"  # EventUpdated while OPEN/ADMITTING: config frozen
    STALE = "stale"  # event_version <= stored
    IGNORED = "ignored"  # nothing to do (e.g. already terminal)
    UNKNOWN_EVENT = "unknown_event"  # no config row for the event
    CANCELLED = "cancelled"
    CLOSED = "closed"  # sold out
    REAPPLIED = "reapplied"  # duplicate: Redis side effects re-run, MySQL untouched
    RELEASED = "released"  # OrderConfirmed freed a slot
    NOT_FOUND = "not_found"  # OrderConfirmed for an unknown/ended admission
    EXPIRED = "expired"  # OrderConfirmed after the deadline, before the sweep
    MISMATCH = "mismatch"  # OrderConfirmed user/event differ: integrity error


@dataclass(frozen=True, slots=True)
class HandleResult:
    outcome: Outcome
    admitted: int = 0  # buyers admitted straight away (OrderConfirmed)


class MessageHandlers:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis: Redis,
        configs: ConfigRepository,
        queues: QueueRepository,
    ):
        self._sessions = sessions
        self._redis = redis
        self._configs = configs
        self._queues = queues

    async def handle(self, message: Message) -> HandleResult:
        payload = message.payload
        match payload:
            case EventPublished():
                result = await self._event_config(payload, published=True)
            case EventUpdated():
                result = await self._event_config(payload, published=False)
            case EventCancelled():
                result = await self._event_cancelled(payload)
            case EventSoldOut():
                result = await self._event_sold_out(payload)
            case OrderConfirmed():
                result = await self._order_confirmed(payload)
        log.info(
            "message handled",
            extra={
                "message_id": message.message_id,
                "message_type": message.message_type,
                "event_id": payload.event_id,
                "outcome": result.outcome.value,
            },
        )
        return result

    # --- EventPublished / EventUpdated ---------------------------------------------------

    async def _event_config(
        self, payload: EventPublished | EventUpdated, *, published: bool
    ) -> HandleResult:
        now = await server_now(self._redis)
        async with self._sessions.begin() as session:
            row = await _lock(session, payload.event_id)
            if row is None:
                # EventPublished, or EventUpdated arriving first: it carries the
                # full current values, so it's an upsert (decision 8).
                row = QueueConfig(
                    event_id=payload.event_id,
                    status=QueueStatus.SCHEDULED,
                    closed_at=None,
                )
                _apply_config(row, payload)
                session.add(row)
                outcome = Outcome.CREATED
            elif payload.event_version <= row.event_version:
                outcome = Outcome.STALE
            else:
                status = effective_status(QueueConfigData.from_row(row), now)
                if status is QueueStatus.SCHEDULED:
                    _apply_config(row, payload)
                    outcome = Outcome.UPDATED
                elif status in TERMINAL_STATUSES:
                    outcome = Outcome.IGNORED
                else:
                    # OPEN or ADMITTING: the configuration is frozen. Reporting the
                    # rejection back to Event is out of scope (open point).
                    log.warning(
                        "event_update_rejected",
                        extra={
                            "event_id": payload.event_id,
                            "queue_status": status.value,
                            "stored_version": row.event_version,
                            "incoming_version": payload.event_version,
                            "stored": _config_summary(row),
                            "incoming": payload.model_dump(mode="json"),
                        },
                    )
                    outcome = Outcome.REJECTED
            await session.flush()
            cfg = QueueConfigData.from_row(row)
        # After commit. Always refresh: harmless when unchanged, and it repairs a
        # cache write lost before a redelivery. The cache script refuses stale data.
        await self._configs.upsert_cache(cfg)
        if not published and outcome is Outcome.CREATED:
            log.info("event_updated_before_published", extra={"event_id": payload.event_id})
        return HandleResult(outcome)

    # --- EventCancelled -------------------------------------------------------------------

    async def _event_cancelled(self, payload: EventCancelled) -> HandleResult:
        async with self._sessions.begin() as session:
            row = await _lock(session, payload.event_id)
            if row is None:
                return HandleResult(Outcome.UNKNOWN_EVENT)
            if row.status is QueueStatus.CANCELLED:
                outcome = Outcome.REAPPLIED  # duplicate: re-run the Redis cleanup below
            elif payload.event_version <= row.event_version:
                log.warning("stale_event_cancelled", extra={"event_id": payload.event_id})
                return HandleResult(Outcome.STALE)
            else:
                # Allowed from any status, including CLOSED (sold out, then cancelled).
                row.status = QueueStatus.CANCELLED
                row.closed_at = payload.cancelled_at
                row.event_version = payload.event_version
                outcome = Outcome.CANCELLED
            await session.flush()
            cfg = QueueConfigData.from_row(row)
        # Terminal status into the cache *before* deleting keys, so the join/admit
        # scripts already refuse new work while cleanup runs.
        await self._configs.upsert_cache(cfg)
        await self._queues.cancel_event(payload.event_id)
        return HandleResult(outcome)

    # --- EventSoldOut ------------------------------------------------------------------------

    async def _event_sold_out(self, payload: EventSoldOut) -> HandleResult:
        async with self._sessions.begin() as session:
            row = await _lock(session, payload.event_id)
            if row is None:
                return HandleResult(Outcome.UNKNOWN_EVENT)
            if row.status is QueueStatus.CANCELLED:
                return HandleResult(Outcome.IGNORED)
            if row.status is QueueStatus.CLOSED:
                outcome = Outcome.REAPPLIED  # duplicate, or the sale had already ended
            else:
                row.status = QueueStatus.CLOSED
                row.closed_at = payload.sold_out_at
                outcome = Outcome.CLOSED
            await session.flush()
            cfg = QueueConfigData.from_row(row)
        await self._configs.upsert_cache(cfg)
        await self._queues.close_event(payload.event_id)
        return HandleResult(outcome)

    # --- OrderConfirmed --------------------------------------------------------------------

    async def _order_confirmed(self, payload: OrderConfirmed) -> HandleResult:
        """Redis only: release the admission, then admit the next buyer at once."""
        release = await self._queues.release(
            payload.admission_id, payload.user_id, payload.event_id
        )
        admitted = 0
        match release.outcome:
            case ReleaseOutcome.RELEASED:
                outcome = Outcome.RELEASED
                admitted = await self._admit_now(payload.event_id)
            case ReleaseOutcome.MISMATCH:
                outcome = Outcome.MISMATCH
                log.error(
                    "admission_integrity_mismatch",
                    extra={
                        "admission_id": payload.admission_id,
                        "order_id": payload.order_id,
                        "message_user_id": payload.user_id,
                        "message_event_id": payload.event_id,
                        "stored_user_id": release.stored_user_id,
                        "stored_event_id": release.stored_event_id,
                    },
                )
            case ReleaseOutcome.EXPIRED:
                outcome = Outcome.EXPIRED  # the admitter's sweep frees the slot
            case ReleaseOutcome.NOT_FOUND:
                outcome = Outcome.NOT_FOUND
        return HandleResult(outcome, admitted=admitted)

    async def _admit_now(self, event_id: str) -> int:
        cfg = await self._configs.get(event_id)
        if cfg is None:
            return 0
        if effective_status(cfg, await server_now(self._redis)) is not QueueStatus.ADMITTING:
            return 0  # sold out / ended / cancelled: the freed slot stays empty
        return len((await self._queues.admit(cfg)).admitted)


async def _lock(session: AsyncSession, event_id: str) -> QueueConfig | None:
    result = await session.execute(
        select(QueueConfig).where(QueueConfig.event_id == event_id).with_for_update()
    )
    return result.scalar_one_or_none()


def _apply_config(row: QueueConfig, payload: EventPublished | EventUpdated) -> None:
    row.event_version = payload.event_version
    row.total_capacity = payload.total_capacity
    row.active_pool_capacity = active_pool_capacity(payload.total_capacity)
    row.release_at = payload.release_at
    row.queue_opens_at = payload.queue_join_opens_at
    row.sale_ends_at = payload.sale_ends_at  # full current values: None clears it


def _config_summary(row: QueueConfig) -> dict[str, object]:
    return {
        "event_version": row.event_version,
        "total_capacity": row.total_capacity,
        "queue_opens_at": row.queue_opens_at.isoformat(),
        "release_at": row.release_at.isoformat(),
        "sale_ends_at": row.sale_ends_at.isoformat() if row.sale_ends_at else None,
    }
