"""Queue configuration: MySQL (source of truth) with a read-through Redis cache."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import resources

from redis.asyncio import Redis
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.cursor import InvalidCursor, decode_cursor, encode_cursor
from app.core.db import QueueConfig
from app.core.status import QueueStatus
from app.core.timefmt import from_ms, to_ms
from app.queue import keys

# Per-event keys outlive the release by this much, so a failed cleanup never leaks.
SAFETY_TTL_AFTER_RELEASE = timedelta(days=3)
MAX_PAGE_SIZE = 200

# Allowed previous statuses for each target. Terminal statuses are never left,
# except CLOSED -> CANCELLED (a sold-out event can still be cancelled).
_ALLOWED_FROM: dict[QueueStatus, frozenset[QueueStatus]] = {
    QueueStatus.OPEN: frozenset({QueueStatus.SCHEDULED}),
    QueueStatus.ADMITTING: frozenset({QueueStatus.SCHEDULED, QueueStatus.OPEN}),
    QueueStatus.CLOSED: frozenset({QueueStatus.SCHEDULED, QueueStatus.OPEN, QueueStatus.ADMITTING}),
    QueueStatus.CANCELLED: frozenset(
        {QueueStatus.SCHEDULED, QueueStatus.OPEN, QueueStatus.ADMITTING, QueueStatus.CLOSED}
    ),
}


@dataclass(frozen=True, slots=True)
class QueueConfigData:
    event_id: str
    event_version: int
    total_capacity: int
    active_pool_capacity: int
    queue_opens_at: datetime
    release_at: datetime
    status: QueueStatus
    closed_at: datetime | None
    sale_ends_at: datetime | None = None  # optional end of sale; None = never ends

    @classmethod
    def from_row(cls, row: QueueConfig) -> QueueConfigData:
        return cls(
            event_id=row.event_id,
            event_version=row.event_version,
            total_capacity=row.total_capacity,
            active_pool_capacity=row.active_pool_capacity,
            queue_opens_at=row.queue_opens_at,
            release_at=row.release_at,
            status=row.status,
            closed_at=row.closed_at,
            sale_ends_at=row.sale_ends_at,
        )

    def to_cache(self) -> dict[str, str]:
        return {
            "event_id": self.event_id,
            "event_version": str(self.event_version),
            "total_capacity": str(self.total_capacity),
            "active_pool_capacity": str(self.active_pool_capacity),
            "queue_opens_at": str(to_ms(self.queue_opens_at)),
            "release_at": str(to_ms(self.release_at)),
            "status": self.status.value,
            "closed_at": "" if self.closed_at is None else str(to_ms(self.closed_at)),
            "sale_ends_at": "" if self.sale_ends_at is None else str(to_ms(self.sale_ends_at)),
        }

    @classmethod
    def from_cache(cls, raw: dict[str, str]) -> QueueConfigData:
        return cls(
            event_id=raw["event_id"],
            event_version=int(raw["event_version"]),
            total_capacity=int(raw["total_capacity"]),
            active_pool_capacity=int(raw["active_pool_capacity"]),
            queue_opens_at=from_ms(int(raw["queue_opens_at"])),
            release_at=from_ms(int(raw["release_at"])),
            status=QueueStatus(raw["status"]),
            closed_at=from_ms(int(raw["closed_at"])) if raw.get("closed_at") else None,
            sale_ends_at=from_ms(int(raw["sale_ends_at"])) if raw.get("sale_ends_at") else None,
        )


@dataclass(frozen=True, slots=True)
class ConfigPage:
    items: list[QueueConfigData]
    next_cursor: str | None


class ConfigRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], redis: Redis):
        self._sessions = sessions
        self._redis = redis
        script = resources.files("app.queue.lua").joinpath("cache_config.lua").read_text()
        self._cache_script = redis.register_script(script)

    async def get(self, event_id: str) -> QueueConfigData | None:
        """Cached config; on a cache miss, load from MySQL and repopulate."""
        # decode_responses=True, so keys and values are str.
        raw: dict[str, str] = await self._redis.hgetall(keys.config(event_id))  # type: ignore[assignment]
        if raw:
            return QueueConfigData.from_cache(raw)
        cfg = await self.load(event_id)
        if cfg is not None:
            await self.upsert_cache(cfg)
        return cfg

    async def load(self, event_id: str) -> QueueConfigData | None:
        """Read straight from MySQL, bypassing the cache."""
        async with self._sessions() as session:
            row = await session.get(QueueConfig, event_id)
            return None if row is None else QueueConfigData.from_row(row)

    async def upsert_cache(self, cfg: QueueConfigData) -> bool:
        """Write cfg to the cache unless the cached copy is newer or terminal.

        Returns False when the write was refused as stale.
        """
        expire_at = to_ms(cfg.release_at + SAFETY_TTL_AFTER_RELEASE)
        args: list[str] = [str(expire_at)]
        for field, value in cfg.to_cache().items():
            args += [field, value]
        written = await self._cache_script(keys=[keys.config(cfg.event_id)], args=args)
        return bool(written)

    async def set_status(
        self, event_id: str, status: QueueStatus, closed_at: datetime | None = None
    ) -> bool:
        """Move to status if the transition is allowed, then refresh the cache.

        Returns False (and changes nothing) for a disallowed transition, e.g.
        persisting ADMITTING after the event was already CLOSED.
        """
        allowed_from = _ALLOWED_FROM.get(status)
        if allowed_from is None:
            raise ValueError(f"cannot transition to {status}")
        values: dict[str, object] = {"status": status}
        if closed_at is not None:
            values["closed_at"] = closed_at
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(QueueConfig)
                .where(QueueConfig.event_id == event_id, QueueConfig.status.in_(allowed_from))
                .values(**values)
            )
            changed = bool(result.rowcount > 0)  # type: ignore[attr-defined]
        if changed:
            cfg = await self.load(event_id)
            if cfg is not None:
                await self.upsert_cache(cfg)
        return changed

    async def list_configs(
        self, status: QueueStatus | None, limit: int, cursor: str | None
    ) -> ConfigPage:
        """Page through configs ordered by (release_at, event_id).

        Raises InvalidCursor for a malformed or tampered cursor.
        """
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        query = select(QueueConfig)
        if status is not None:
            query = query.where(QueueConfig.status == status)
        if cursor is not None:
            after_release, after_event = _parse_list_cursor(cursor)
            query = query.where(
                or_(
                    QueueConfig.release_at > after_release,
                    and_(
                        QueueConfig.release_at == after_release,
                        QueueConfig.event_id > after_event,
                    ),
                )
            )
        query = query.order_by(QueueConfig.release_at, QueueConfig.event_id).limit(limit + 1)
        async with self._sessions() as session:
            rows = (await session.scalars(query)).all()
        items = [QueueConfigData.from_row(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = encode_cursor({"r": to_ms(last.release_at), "e": last.event_id})
        return ConfigPage(items=items, next_cursor=next_cursor)

    async def due_for_open(self, now: datetime) -> list[QueueConfigData]:
        """SCHEDULED events whose queue_opens_at has passed."""
        query = (
            select(QueueConfig)
            .where(
                QueueConfig.status == QueueStatus.SCHEDULED,
                QueueConfig.queue_opens_at <= now,
            )
            .order_by(QueueConfig.release_at)
        )
        async with self._sessions() as session:
            rows = (await session.scalars(query)).all()
        return [QueueConfigData.from_row(row) for row in rows]

    async def live_or_due(self, now: datetime) -> list[QueueConfigData]:
        """Every event the admitter should be working on: SCHEDULED events whose
        lobby has opened, plus all persisted OPEN/ADMITTING events.

        One indexed query. OPEN/ADMITTING rows always satisfy the queue_opens_at
        bound too (their config is frozen once the lobby opens), so the same
        condition covers both, and re-listing live events lets the admitter
        rebuild queue:live_events if Redis ever loses it.
        """
        query = (
            select(QueueConfig)
            .where(
                QueueConfig.status.in_(
                    (QueueStatus.SCHEDULED, QueueStatus.OPEN, QueueStatus.ADMITTING)
                ),
                QueueConfig.queue_opens_at <= now,
            )
            .order_by(QueueConfig.release_at)
        )
        async with self._sessions() as session:
            rows = (await session.scalars(query)).all()
        return [QueueConfigData.from_row(row) for row in rows]


def _parse_list_cursor(cursor: str) -> tuple[datetime, str]:
    data = decode_cursor(cursor)
    release_ms, event_id = data.get("r"), data.get("e")
    if not isinstance(release_ms, int) or not isinstance(event_id, str):
        raise InvalidCursor("malformed cursor")
    return from_ms(release_ms), event_id
