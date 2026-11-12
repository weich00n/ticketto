"""Readiness against real dependencies, and with each one unreachable.

The "down" cases point the app at a closed port (no mocks), so they need no
test services at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.main import create_app
from tests.conftest import MySQLInfo
from tests.fixtures.keys import TestKey
from tests.integration.api.conftest import Api, api_settings

pytestmark = pytest.mark.integration

CLOSED_REDIS = "redis://127.0.0.1:1/0"
CLOSED_MYSQL = "mysql+asyncmy://queue:queue@127.0.0.1:1/queue_service"


@asynccontextmanager
async def _client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def test_ready_when_dependencies_are_up(api: Api) -> None:
    resp = await api.client.get("/health/ready")
    assert (resp.status_code, resp.json()) == (200, {"status": "ready"})


async def test_live_ignores_dependencies(signing_key: TestKey, tmp_path: Path) -> None:
    settings = api_settings(
        CLOSED_MYSQL, CLOSED_REDIS, signing_key.write_private(tmp_path), signing_key.kid
    )
    async with _client(settings) as client:
        resp = await client.get("/health/live")
    assert (resp.status_code, resp.json()) == (200, {"status": "ok"})


async def test_both_down(signing_key: TestKey, tmp_path: Path) -> None:
    settings = api_settings(
        CLOSED_MYSQL, CLOSED_REDIS, signing_key.write_private(tmp_path), signing_key.kid
    )
    async with _client(settings) as client:
        resp = await client.get("/health/ready")
    assert (resp.status_code, resp.json()) == (
        503,
        {"status": "unavailable", "failed": ["mysql", "redis"]},
    )


async def test_redis_down(mysql_info: MySQLInfo, signing_key: TestKey, tmp_path: Path) -> None:
    settings = api_settings(
        mysql_info.async_url, CLOSED_REDIS, signing_key.write_private(tmp_path), "k"
    )
    async with _client(settings) as client:
        resp = await client.get("/health/ready")
    assert (resp.status_code, resp.json()) == (503, {"status": "unavailable", "failed": ["redis"]})


async def test_mysql_down(redis_url: str, signing_key: TestKey, tmp_path: Path) -> None:
    settings = api_settings(CLOSED_MYSQL, redis_url, signing_key.write_private(tmp_path), "k")
    async with _client(settings) as client:
        resp = await client.get("/health/ready")
    assert (resp.status_code, resp.json()) == (503, {"status": "unavailable", "failed": ["mysql"]})
