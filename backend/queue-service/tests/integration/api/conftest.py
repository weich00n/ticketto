"""The real app (lifespan, Redis, MySQL, keys) behind an in-process HTTP client,
plus helpers that put an event into a given lifecycle state."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from random import Random

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import Services
from app.core.config import Settings
from app.core.redis import server_now
from app.core.status import QueueStatus
from app.main import create_app
from app.queue.config_repo import QueueConfigData
from tests.conftest import MySQLInfo
from tests.fixtures.factories import insert_config
from tests.fixtures.keys import TestKey


def api_settings(mysql_url: str, redis_url: str, key_path: Path, kid: str) -> Settings:
    return Settings(
        mysql_url=mysql_url,
        redis_url=redis_url,
        rabbitmq_url="amqp://unused",  # the API never talks to RabbitMQ
        jwt_private_key_path=key_path,
        jwt_kid=kid,
        jwt_public_keys_dir=None,
    )


@dataclass
class Api:
    client: httpx.AsyncClient
    services: Services
    redis: Redis
    sessions: async_sessionmaker[AsyncSession]

    @staticmethod
    def headers(user_id: str) -> dict[str, str]:
        return {"X-User-Sub": user_id}

    async def event(
        self,
        *,
        release_in: timedelta,
        capacity: int = 2,
        status: QueueStatus = QueueStatus.SCHEDULED,
        sale_ends_in: timedelta | None = None,
    ) -> QueueConfigData:
        """A queue_configs row whose release_at (and sale_ends_at) are relative
        to Redis TIME."""
        now = await server_now(self.redis)
        event_id = await insert_config(
            self.sessions,
            release_at=now + release_in,
            total_capacity=capacity * 5,
            status=status,
            sale_ends_at=None if sale_ends_in is None else now + sale_ends_in,
        )
        cfg = await self.services.configs.get(event_id)
        assert cfg is not None
        return cfg

    async def admitting_event(
        self,
        *,
        capacity: int = 2,
        users: Sequence[str] = (),
        sale_ends_in: timedelta | None = None,
    ) -> QueueConfigData:
        """Released 1 s ago and shuffled; `users` joined in FIFO order."""
        cfg = await self.event(
            release_in=-timedelta(seconds=1),
            capacity=capacity,
            status=QueueStatus.ADMITTING,
            sale_ends_in=sale_ends_in,
        )
        await self.services.queues.shuffle(cfg, Random(0))
        for user in users:
            await self.services.queues.join(cfg, user)
        return cfg

    async def sell_out(self, cfg: QueueConfigData) -> None:
        """What the EventSoldOut handler will do (Phase 7)."""
        assert await self.services.configs.set_status(cfg.event_id, QueueStatus.CLOSED)
        await self.services.queues.close_event(cfg.event_id)

    async def cancel(self, cfg: QueueConfigData) -> None:
        """What the EventCancelled handler will do (Phase 7)."""
        assert await self.services.configs.set_status(cfg.event_id, QueueStatus.CANCELLED)
        await self.services.queues.cancel_event(cfg.event_id)


@pytest.fixture
async def api(
    migrated_mysql: MySQLInfo,
    redis_url: str,
    redis: Redis,
    sessions: async_sessionmaker[AsyncSession],
    signing_key: TestKey,
    tmp_path: Path,
) -> AsyncIterator[Api]:
    settings = api_settings(
        migrated_mysql.async_url, redis_url, signing_key.write_private(tmp_path), signing_key.kid
    )
    app = create_app(settings, rng=Random(0))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield Api(client, app.state.services, redis, sessions)
