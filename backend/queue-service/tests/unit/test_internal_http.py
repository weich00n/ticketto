"""HTTP layer of the internal, JWKS and liveness endpoints without Redis/MySQL.

AdminService is replaced by a stub; JWKS uses real keys. Readiness needs real
connections and is covered in tests/integration/api/test_health.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.api.deps import get_admin_service, get_tokens
from app.api.errors import AdmissionNotFound, EventNotFound
from app.core.config import Settings
from app.core.cursor import InvalidCursor
from app.core.status import QueueStatus
from app.core.tokens import AdmissionTokens, load_private_key
from app.main import create_app
from app.queue.admin import QueueDetail, QueuePage, QueueSummary, clamp_limit
from app.queue.models import Admission, AdmissionPage, QueueCounts
from tests.fixtures.factories import config_data
from tests.fixtures.keys import TestKey

pytestmark = pytest.mark.unit

EVENT = "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90"
RELEASE = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
CFG = config_data(
    event_id=EVENT,
    event_version=3,
    total_capacity=304,
    release_at=RELEASE,
    sale_ends_at=RELEASE + timedelta(days=7),
    status=QueueStatus.ADMITTING,
)
ADMISSION = Admission(
    admission_id="8b0e5c1a-4f2d-4c1e-9a77-2d6f0b3e9c41",
    user_id="2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33",
    event_id=EVENT,
    admitted_at=RELEASE,
    selection_expires_at=RELEASE + timedelta(minutes=5),
    admission_expires_at=RELEASE + timedelta(minutes=11),
)


class StubAdmin:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def list_queues(
        self, status: QueueStatus | None, limit: int, cursor: str | None
    ) -> QueuePage:
        self.calls.append(("list_queues", status, limit, cursor))
        if cursor == "bad":
            raise InvalidCursor("malformed cursor")
        return QueuePage([QueueSummary(CFG, QueueStatus.ADMITTING)], next_cursor="next")

    async def queue_detail(self, event_id: str) -> QueueDetail:
        if event_id != EVENT:
            raise EventNotFound()
        return QueueDetail(CFG, QueueStatus.ADMITTING, QueueCounts(waiting=4821, active=60))

    async def admissions(self, event_id: str, limit: int, cursor: str | None) -> AdmissionPage:
        self.calls.append(("admissions", event_id, limit, cursor))
        return AdmissionPage([ADMISSION], next_cursor=None)

    async def admission(self, admission_id: str) -> Admission:
        if admission_id != ADMISSION.admission_id:
            raise AdmissionNotFound()
        return ADMISSION


@pytest.fixture
def stub() -> StubAdmin:
    return StubAdmin()


@pytest.fixture
async def client(
    stub: StubAdmin, signing_key: TestKey, tmp_path: Path
) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        mysql_url="mysql+asyncmy://u:p@h/db",
        redis_url="redis://h",
        rabbitmq_url="amqp://h",
        jwt_private_key_path=Path("unused.pem"),
        jwt_kid="unused",
    )
    tokens = AdmissionTokens(load_private_key(signing_key.write_private(tmp_path)), "test-key-1")
    app = create_app(settings)  # lifespan not run: no Redis/MySQL needed
    app.dependency_overrides[get_admin_service] = lambda: stub
    app.dependency_overrides[get_tokens] = lambda: tokens
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_list_queues(client: httpx.AsyncClient, stub: StubAdmin) -> None:
    resp = await client.get("/internal/v1/queues", params={"status": "ADMITTING", "limit": 10})
    assert resp.status_code == 200
    assert resp.json() == {
        "items": [
            {"event_id": EVENT, "queue_status": "ADMITTING", "release_at": "2026-11-20T12:00:00Z"}
        ],
        "next_cursor": "next",
    }
    assert stub.calls == [("list_queues", QueueStatus.ADMITTING, 10, None)]


async def test_list_queues_defaults(client: httpx.AsyncClient, stub: StubAdmin) -> None:
    await client.get("/internal/v1/queues")
    assert stub.calls == [("list_queues", None, 50, None)]


@pytest.mark.parametrize(
    ("params", "loc"),
    [({"status": "PAUSED"}, ["query", "status"]), ({"limit": "many"}, ["query", "limit"])],
)
async def test_list_queues_bad_params_are_400(
    client: httpx.AsyncClient, params: dict[str, str], loc: list[str]
) -> None:
    resp = await client.get("/internal/v1/queues", params=params)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"]["errors"][0]["loc"] == loc


async def test_bad_cursor_is_400(client: httpx.AsyncClient) -> None:
    resp = await client.get("/internal/v1/queues", params={"cursor": "bad"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"
    assert resp.json()["error"]["details"]["errors"][0]["loc"] == ["query", "cursor"]


@pytest.mark.parametrize(("limit", "expected"), [(-5, 1), (0, 1), (1, 1), (200, 200), (500, 200)])
def test_limit_is_clamped(limit: int, expected: int) -> None:
    assert clamp_limit(limit) == expected


async def test_queue_detail_matches_reference(client: httpx.AsyncClient) -> None:
    resp = await client.get(f"/internal/v1/events/{EVENT}/queue")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "event_id": EVENT,
        "queue_status": "ADMITTING",
        "event_version": 3,
        "total_capacity": 304,
        "active_pool_capacity": 60,
        "queue_opens_at": "2026-11-20T11:45:00Z",
        "release_at": "2026-11-20T12:00:00Z",
        "sale_ends_at": "2026-11-27T12:00:00Z",
        "closed_at": None,
        "waiting_count": 4821,
        "active_count": 60,
    }
    assert list(body) == [
        "event_id",
        "queue_status",
        "event_version",
        "total_capacity",
        "active_pool_capacity",
        "queue_opens_at",
        "release_at",
        "sale_ends_at",
        "closed_at",
        "waiting_count",
        "active_count",
    ]


@pytest.mark.parametrize("event_id", ["00000000-0000-0000-0000-000000000000", "not-a-uuid"])
async def test_queue_detail_unknown_event(client: httpx.AsyncClient, event_id: str) -> None:
    resp = await client.get(f"/internal/v1/events/{event_id}/queue")
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "EVENT_NOT_FOUND")


async def test_list_admissions(client: httpx.AsyncClient, stub: StubAdmin) -> None:
    resp = await client.get(f"/internal/v1/events/{EVENT}/queue/admissions", params={"limit": 2})
    assert resp.status_code == 200
    assert resp.json() == {"items": [ADMISSION.to_public()], "next_cursor": None}
    assert stub.calls == [("admissions", EVENT, 2, None)]


async def test_get_admission_never_includes_token(client: httpx.AsyncClient) -> None:
    resp = await client.get(f"/internal/v1/admissions/{ADMISSION.admission_id}")
    assert resp.status_code == 200
    assert resp.json() == ADMISSION.to_public()


@pytest.mark.parametrize("admission_id", ["00000000-0000-0000-0000-000000000000", "123"])
async def test_unknown_admission_is_404(client: httpx.AsyncClient, admission_id: str) -> None:
    resp = await client.get(f"/internal/v1/admissions/{admission_id}")
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "ADMISSION_NOT_FOUND")


async def test_jwks(client: httpx.AsyncClient) -> None:
    resp = await client.get("/.well-known/jwks.json")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=300"
    (key,) = resp.json()["keys"]
    assert list(key) == ["kty", "kid", "use", "alg", "n", "e"]
    assert (key["kid"], key["alg"], key["use"]) == ("test-key-1", "RS256", "sig")


async def test_liveness(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health/live")
    assert (resp.status_code, resp.json()) == (200, {"status": "ok"})


def test_openapi_lists_every_endpoint() -> None:
    settings = Settings(
        mysql_url="mysql+asyncmy://u:p@h/db",
        redis_url="redis://h",
        rabbitmq_url="amqp://h",
        jwt_private_key_path=Path("unused.pem"),
        jwt_kid="unused",
    )
    paths = create_app(settings).openapi()["paths"]
    assert sorted((m.upper(), p) for p, ops in paths.items() for m in ops) == sorted(
        [
            ("GET", "/api/v1/events/{event_id}/queue"),
            ("POST", "/api/v1/events/{event_id}/queue/entries"),
            ("GET", "/api/v1/events/{event_id}/queue/me"),
            ("DELETE", "/api/v1/events/{event_id}/queue/me"),
            ("GET", "/internal/v1/queues"),
            ("GET", "/internal/v1/events/{event_id}/queue"),
            ("GET", "/internal/v1/events/{event_id}/queue/admissions"),
            ("GET", "/internal/v1/admissions/{admission_id}"),
            ("GET", "/.well-known/jwks.json"),
            ("GET", "/health/live"),
            ("GET", "/health/ready"),
        ]
    )
