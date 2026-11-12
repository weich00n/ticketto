"""Unreachable Redis or MySQL turns into 503 SERVICE_UNAVAILABLE, not a crash.

Dependencies are made unreachable by pointing the app at a closed port, not by
mocking clients.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.main import create_app
from tests.fixtures.factories import event_id, user_id
from tests.fixtures.keys import TestKey
from tests.integration.api.conftest import api_settings

pytestmark = pytest.mark.integration

CLOSED_PORT_REDIS = "redis://127.0.0.1:1/0"
CLOSED_PORT_MYSQL = "mysql+asyncmy://queue:queue@127.0.0.1:1/queue_service"


@asynccontextmanager
async def _client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _assert_unavailable(resp: httpx.Response) -> None:
    assert resp.status_code == 503
    assert resp.json() == {
        "error": {
            "code": "SERVICE_UNAVAILABLE",
            "message": resp.json()["error"]["message"],
            "details": {},
        }
    }


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("GET", ""), ("POST", "/entries"), ("GET", "/me"), ("DELETE", "/me")],
)
async def test_redis_down(signing_key: TestKey, tmp_path: Path, method: str, suffix: str) -> None:
    settings = api_settings(
        CLOSED_PORT_MYSQL, CLOSED_PORT_REDIS, signing_key.write_private(tmp_path), signing_key.kid
    )
    async with _client(settings) as client:
        resp = await client.request(
            method,
            f"/api/v1/events/{event_id()}/queue{suffix}",
            headers={"X-User-Sub": user_id()},
        )
    _assert_unavailable(resp)


async def test_mysql_down(redis_url: str, signing_key: TestKey, tmp_path: Path) -> None:
    """Redis is up but the config isn't cached, so the lookup reaches MySQL."""
    settings = api_settings(
        CLOSED_PORT_MYSQL, redis_url, signing_key.write_private(tmp_path), signing_key.kid
    )
    async with _client(settings) as client:
        resp = await client.get(f"/api/v1/events/{event_id()}/queue")
    _assert_unavailable(resp)
