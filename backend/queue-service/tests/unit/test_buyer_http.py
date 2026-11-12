"""HTTP layer of the buyer API without Redis/MySQL: routing, identity, status codes
and exact body shapes. QueueService is replaced by a stub returning fixed views;
the real service is covered by tests/integration/api."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.api.deps import current_user, get_queue_service
from app.api.errors import EventNotFound, Unauthenticated
from app.core.config import Settings
from app.core.status import EntryStatus, QueueStatus
from app.main import create_app
from app.queue.models import Admission, Entry
from app.queue.service import EntryView, LobbyView
from tests.fixtures.factories import config_data

pytestmark = pytest.mark.unit

EVENT = "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90"
USER = "2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33"
RELEASE = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
CFG = config_data(event_id=EVENT, release_at=RELEASE, status=QueueStatus.ADMITTING)
ADMISSION = Admission(
    admission_id="8b0e5c1a-4f2d-4c1e-9a77-2d6f0b3e9c41",
    user_id=USER,
    event_id=EVENT,
    admitted_at=RELEASE,
    selection_expires_at=RELEASE + timedelta(minutes=5),
    admission_expires_at=RELEASE + timedelta(minutes=11),
)
VIEWS = {
    "prequeue": EntryView(CFG, QueueStatus.OPEN, Entry(EntryStatus.WAITING), None, 30),
    "waiting": EntryView(CFG, QueueStatus.ADMITTING, Entry(EntryStatus.WAITING, 142), None, 10),
    "admitted": EntryView(
        CFG,
        QueueStatus.ADMITTING,
        Entry(EntryStatus.ADMITTED, admission=ADMISSION),
        "eyJhbGciOiJSUzI1NiIs.payload.sig",
        None,
    ),
    "none": EntryView(CFG, QueueStatus.CLOSED, Entry(EntryStatus.NONE), None, None),
}


class StubService:
    def __init__(self) -> None:
        self.view = VIEWS["none"]
        self.created = True
        self.calls: list[tuple[str, ...]] = []

    async def lobby(self, event_id: str) -> LobbyView:
        self.calls.append(("lobby", event_id))
        if event_id != EVENT:
            raise EventNotFound()
        return LobbyView(CFG, QueueStatus.OPEN, RELEASE - timedelta(minutes=7, seconds=47))

    async def status(self, event_id: str, user_id: str) -> EntryView:
        self.calls.append(("status", event_id, user_id))
        return self.view

    async def join(self, event_id: str, user_id: str) -> tuple[bool, EntryView]:
        self.calls.append(("join", event_id, user_id))
        return self.created, self.view

    async def leave(self, event_id: str, user_id: str) -> None:
        self.calls.append(("leave", event_id, user_id))


@pytest.fixture
def stub() -> StubService:
    return StubService()


@pytest.fixture
async def client(stub: StubService) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(_settings())  # lifespan is not run: no Redis/MySQL/keys needed
    app.dependency_overrides[get_queue_service] = lambda: stub
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _settings() -> Settings:
    return Settings(
        mysql_url="mysql+asyncmy://u:p@h/db",
        redis_url="redis://h",
        rabbitmq_url="amqp://h",
        jwt_private_key_path=Path("unused.pem"),
        jwt_kid="unused",
    )


ME = f"/api/v1/events/{EVENT}/queue/me"
ENTRIES = f"/api/v1/events/{EVENT}/queue/entries"
HEADERS = {"X-User-Sub": USER}


async def test_lobby_shape(client: httpx.AsyncClient) -> None:
    resp = await client.get(f"/api/v1/events/{EVENT}/queue")
    assert resp.status_code == 200
    assert resp.json() == {
        "event_id": EVENT,
        "queue_status": "OPEN",
        "queue_opens_at": "2026-11-20T11:45:00Z",
        "release_at": "2026-11-20T12:00:00Z",
        "sale_ends_at": None,
        "server_time": "2026-11-20T11:52:13Z",
    }


async def test_lobby_needs_no_identity_and_unknown_event_is_404(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/api/v1/events/00000000-0000-0000-0000-000000000000/queue")
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "EVENT_NOT_FOUND")


@pytest.mark.parametrize(
    ("view", "expected"),
    [
        (
            "prequeue",
            {
                "event_id": EVENT,
                "queue_status": "OPEN",
                "entry_status": "WAITING",
                "position": None,
                "release_at": "2026-11-20T12:00:00Z",
                "poll_after_seconds": 30,
            },
        ),
        (
            "waiting",
            {
                "event_id": EVENT,
                "queue_status": "ADMITTING",
                "entry_status": "WAITING",
                "position": 142,
                "poll_after_seconds": 10,
            },
        ),
        (
            "admitted",
            {
                "event_id": EVENT,
                "queue_status": "ADMITTING",
                "entry_status": "ADMITTED",
                "admission": {
                    "admission_id": "8b0e5c1a-4f2d-4c1e-9a77-2d6f0b3e9c41",
                    "user_id": USER,
                    "event_id": EVENT,
                    "admitted_at": "2026-11-20T12:00:00Z",
                    "selection_expires_at": "2026-11-20T12:05:00Z",
                    "admission_expires_at": "2026-11-20T12:11:00Z",
                },
                "admission_token": "eyJhbGciOiJSUzI1NiIs.payload.sig",
                "poll_after_seconds": None,
            },
        ),
        (
            "none",
            {
                "event_id": EVENT,
                "queue_status": "CLOSED",
                "entry_status": "NONE",
                "poll_after_seconds": None,
            },
        ),
    ],
)
async def test_entry_bodies_match_endpoint_reference(
    client: httpx.AsyncClient, stub: StubService, view: str, expected: dict[str, Any]
) -> None:
    stub.view = VIEWS[view]
    resp = await client.get(ME, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == expected
    assert list(resp.json()) == list(expected)  # field order as documented


async def test_join_is_201_when_created_and_200_when_existing(
    client: httpx.AsyncClient, stub: StubService
) -> None:
    stub.view = VIEWS["prequeue"]
    stub.created = True
    assert (await client.post(ENTRIES, headers=HEADERS)).status_code == 201
    stub.created = False
    resp = await client.post(ENTRIES, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["entry_status"] == "WAITING"


async def test_leave_is_204_without_body(client: httpx.AsyncClient, stub: StubService) -> None:
    resp = await client.delete(ME, headers=HEADERS)
    assert resp.status_code == 204
    assert resp.content == b""
    assert stub.calls == [("leave", EVENT, USER)]


@pytest.mark.parametrize(("method", "path"), [("POST", ENTRIES), ("GET", ME), ("DELETE", ME)])
@pytest.mark.parametrize("header", [None, "", "not-a-uuid", "12345"])
async def test_identity_required(
    client: httpx.AsyncClient, stub: StubService, method: str, path: str, header: str | None
) -> None:
    headers = {} if header is None else {"X-User-Sub": header}
    resp = await client.request(method, path, headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHENTICATED"
    assert stub.calls == []


async def test_user_id_is_canonicalised(client: httpx.AsyncClient, stub: StubService) -> None:
    await client.get(ME, headers={"X-User-Sub": USER.upper()})
    assert stub.calls == [("status", EVENT, USER)]


async def test_event_id_is_canonicalised(client: httpx.AsyncClient, stub: StubService) -> None:
    await client.get(f"/api/v1/events/{EVENT.upper()}/queue/me", headers=HEADERS)
    assert stub.calls == [("status", EVENT, USER)]


@pytest.mark.parametrize("path", ["/api/v1/events/nope/queue", "/api/v1/events/nope/queue/me"])
async def test_malformed_event_id_is_event_not_found(client: httpx.AsyncClient, path: str) -> None:
    resp = await client.get(path, headers=HEADERS)
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "EVENT_NOT_FOUND")


async def test_current_user_dependency() -> None:
    assert await current_user(f"  {USER.upper()} ") == USER
    for bad in (None, "", "x"):
        with pytest.raises(Unauthenticated):
            await current_user(bad)
