from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.exc import InterfaceError, OperationalError

from app.api.errors import (
    AdmissionNotFound,
    AlreadyAdmitted,
    AppError,
    EventCancelled,
    EventNotFound,
    EventSoldOut,
    NotInQueue,
    QueueNotOpen,
    SaleEnded,
    ServiceUnavailable,
    Unauthenticated,
    register_error_handlers,
)

pytestmark = pytest.mark.unit

EVENT_ID = "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90"
_RAISERS: dict[str, Exception] = {
    "unauthenticated": Unauthenticated(),
    "event-not-found": EventNotFound(),
    "not-in-queue": NotInQueue(),
    "admission-not-found": AdmissionNotFound(),
    "queue-not-open": QueueNotOpen(datetime(2026, 11, 20, 11, 45, tzinfo=UTC)),
    "already-admitted": AlreadyAdmitted(),
    "sold-out": EventSoldOut(),
    "cancelled": EventCancelled(),
    "sale-ended": SaleEnded(),
    "unavailable": ServiceUnavailable(),
    "redis-down": RedisConnectionError("refused"),
    "redis-timeout": RedisTimeoutError("timeout"),
    "mysql-down": OperationalError("SELECT 1", {}, Exception("gone away")),
    "mysql-interface": InterfaceError("SELECT 1", {}, Exception("closed")),
    "bug": RuntimeError("boom"),
}


def _make_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/raise/{name}")
    async def raise_(name: str) -> None:
        raise _RAISERS[name]

    @app.get("/events/{event_id}/queue")
    async def by_event(event_id: UUID) -> dict[str, str]:
        return {"event_id": str(event_id)}

    @app.get("/admissions/{admission_id}")
    async def by_admission(admission_id: UUID) -> dict[str, str]:
        return {"admission_id": str(admission_id)}

    @app.get("/queues")
    async def queues(limit: int = 50) -> dict[str, int]:
        return {"limit": limit}

    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_make_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _error(resp: httpx.Response) -> dict[str, Any]:
    body = resp.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["message"]
    error: dict[str, Any] = body["error"]
    return error


@pytest.mark.parametrize(
    ("name", "status", "code"),
    [
        ("unauthenticated", 401, "UNAUTHENTICATED"),
        ("event-not-found", 404, "EVENT_NOT_FOUND"),
        ("not-in-queue", 404, "NOT_IN_QUEUE"),
        ("admission-not-found", 404, "ADMISSION_NOT_FOUND"),
        ("already-admitted", 409, "ALREADY_ADMITTED"),
        ("sold-out", 410, "EVENT_SOLD_OUT"),
        ("cancelled", 410, "EVENT_CANCELLED"),
        ("sale-ended", 410, "SALE_ENDED"),
        ("unavailable", 503, "SERVICE_UNAVAILABLE"),
    ],
)
async def test_app_errors_render_contract_body(
    client: httpx.AsyncClient, name: str, status: int, code: str
) -> None:
    resp = await client.get(f"/raise/{name}")
    assert resp.status_code == status
    error = _error(resp)
    assert error["code"] == code
    assert error["details"] == {}


async def test_queue_not_open_carries_opens_at(client: httpx.AsyncClient) -> None:
    resp = await client.get("/raise/queue-not-open")
    assert resp.status_code == 409
    error = _error(resp)
    assert error["code"] == "QUEUE_NOT_OPEN"
    assert error["details"] == {"queue_opens_at": "2026-11-20T11:45:00Z"}
    assert "2026-11-20T11:45:00Z" in error["message"]


@pytest.mark.parametrize("name", ["redis-down", "redis-timeout", "mysql-down", "mysql-interface"])
async def test_dependency_failures_become_503(client: httpx.AsyncClient, name: str) -> None:
    resp = await client.get(f"/raise/{name}")
    assert resp.status_code == 503
    assert _error(resp)["code"] == "SERVICE_UNAVAILABLE"


async def test_malformed_event_id_is_event_not_found(client: httpx.AsyncClient) -> None:
    resp = await client.get("/events/not-a-uuid/queue")
    assert resp.status_code == 404
    assert _error(resp)["code"] == "EVENT_NOT_FOUND"
    assert (await client.get(f"/events/{EVENT_ID}/queue")).status_code == 200


async def test_malformed_admission_id_is_admission_not_found(client: httpx.AsyncClient) -> None:
    resp = await client.get("/admissions/123")
    assert resp.status_code == 404
    assert _error(resp)["code"] == "ADMISSION_NOT_FOUND"


async def test_other_validation_errors_are_400(client: httpx.AsyncClient) -> None:
    resp = await client.get("/queues", params={"limit": "lots"})
    assert resp.status_code == 400
    error = _error(resp)
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"]["errors"][0]["loc"] == ["query", "limit"]


async def test_unknown_route_and_method_use_error_body(client: httpx.AsyncClient) -> None:
    resp = await client.get("/nope")
    assert resp.status_code == 404
    assert _error(resp)["code"] == "NOT_FOUND"
    resp = await client.post("/queues")
    assert resp.status_code == 405
    assert _error(resp)["code"] == "METHOD_NOT_ALLOWED"


async def test_unhandled_exception_is_500_without_leaking(client: httpx.AsyncClient) -> None:
    resp = await client.get("/raise/bug")
    assert resp.status_code == 500
    error = _error(resp)
    assert error["code"] == "INTERNAL_ERROR"
    assert "boom" not in error["message"]


def test_custom_message_and_details() -> None:
    err = EventNotFound("custom", {"event_id": EVENT_ID})
    assert isinstance(err, AppError)
    assert (err.message, err.details) == ("custom", {"event_id": EVENT_ID})
