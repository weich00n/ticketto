"""Read-only views for the internal API (/internal/v1). Nothing here changes a queue."""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis

from app.api.errors import AdmissionNotFound, EventNotFound
from app.core.redis import server_now
from app.core.status import QueueStatus, effective_status
from app.queue.config_repo import MAX_PAGE_SIZE, ConfigRepository, QueueConfigData
from app.queue.models import Admission, AdmissionPage, QueueCounts
from app.queue.repository import QueueRepository

DEFAULT_PAGE_SIZE = 50


def clamp_limit(limit: int) -> int:
    """Page sizes outside 1..200 are clamped rather than refused."""
    return max(1, min(limit, MAX_PAGE_SIZE))


@dataclass(frozen=True, slots=True)
class QueueSummary:
    cfg: QueueConfigData
    queue_status: QueueStatus  # effective (time-derived), not the persisted value


@dataclass(frozen=True, slots=True)
class QueuePage:
    items: list[QueueSummary]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class QueueDetail:
    cfg: QueueConfigData
    queue_status: QueueStatus
    counts: QueueCounts


class AdminService:
    def __init__(self, configs: ConfigRepository, queues: QueueRepository, redis: Redis):
        self._configs = configs
        self._queues = queues
        self._redis = redis

    async def list_queues(
        self, status: QueueStatus | None, limit: int, cursor: str | None
    ) -> QueuePage:
        """Filter on the *persisted* status (indexed; may lag the derived status by
        up to one admitter tick). Each item reports the effective status."""
        now = await server_now(self._redis)
        page = await self._configs.list_configs(status, clamp_limit(limit), cursor)
        return QueuePage(
            items=[QueueSummary(cfg, effective_status(cfg, now)) for cfg in page.items],
            next_cursor=page.next_cursor,
        )

    async def queue_detail(self, event_id: str) -> QueueDetail:
        now = await server_now(self._redis)
        cfg = await self._require_config(event_id)
        counts = await self._queues.counts(event_id, now)
        return QueueDetail(cfg, effective_status(cfg, now), counts)

    async def admissions(self, event_id: str, limit: int, cursor: str | None) -> AdmissionPage:
        now = await server_now(self._redis)
        await self._require_config(event_id)
        return await self._queues.list_admissions(event_id, now, clamp_limit(limit), cursor)

    async def admission(self, admission_id: str) -> Admission:
        now = await server_now(self._redis)
        admission = await self._queues.get_admission(admission_id, now)
        if admission is None:
            raise AdmissionNotFound()
        return admission

    async def _require_config(self, event_id: str) -> QueueConfigData:
        cfg = await self._configs.get(event_id)
        if cfg is None:
            raise EventNotFound()
        return cfg
