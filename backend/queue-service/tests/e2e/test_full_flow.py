"""End to end: one event's whole life and a cancelled second event, driven only
through the HTTP API and the fake services' RabbitMQ messages.

EventPublished -> lobby OPEN -> buyers join the pre-queue -> release -> shuffle,
pool filled -> tokens validate against the served JWKS -> late joiner queues FIFO
-> OrderConfirmed admits the next buyer at once -> deadline expiry refills the
pool -> EventSoldOut: waiting buyers out, admitted keep their admission, newcomers
410 -> second event: EventCancelled -> 410 EVENT_CANCELLED.

Deadlines are shortened (selection 3 s, admission 6 s), so this takes ~15 s.
An admitted buyer who polls after the selection deadline is requeued, so the
deadline expiry is watched through the internal admission endpoint, not by
polling as that buyer.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.redis import server_now
from app.core.status import QueueStatus
from tests.e2e.conftest import ADMISSION_SECONDS, System
from tests.fakes import messages
from tests.fakes.reservation_service import validate_admission_token
from tests.fixtures.factories import event_id, user_id
from tests.integration.messaging.conftest import eventually

pytestmark = pytest.mark.e2e


async def _me(system: System, eid: str, user: str) -> dict[str, Any]:
    resp = await system.client.get(f"/api/v1/events/{eid}/queue/me", headers=system.headers(user))
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _join(system: System, eid: str, user: str) -> tuple[int, dict[str, Any]]:
    resp = await system.client.post(
        f"/api/v1/events/{eid}/queue/entries", headers=system.headers(user)
    )
    return resp.status_code, resp.json()


def _lobby_is(system: System, eid: str, status: str) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        resp = await system.client.get(f"/api/v1/events/{eid}/queue")
        return resp.status_code == 200 and bool(resp.json()["queue_status"] == status)

    return check


def _admission_gone(system: System, admission_id: str) -> Callable[[], Awaitable[bool]]:
    """Read-only (unlike the buyer's own poll, which would requeue a lapsed buyer)."""

    async def check() -> bool:
        resp = await system.client.get(f"/internal/v1/admissions/{admission_id}")
        return resp.status_code == 404

    return check


def _entry_is(system: System, eid: str, user: str, status: str) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return bool((await _me(system, eid, user))["entry_status"] == status)

    return check


async def test_full_lifecycle(system: System) -> None:
    eid = event_id()
    release_at = await server_now(system.redis) + timedelta(seconds=4)

    # --- Event publishes; the lobby opens straight away (release < 15 min away) ---
    system.events.publish(messages.event_published(eid, total_capacity=10, release_at=release_at))
    await eventually(_lobby_is(system, eid, "OPEN"))

    # --- Five buyers join the unordered pre-queue -----------------------------------
    buyers = [user_id() for _ in range(5)]
    for buyer in buyers:
        status, body = await _join(system, eid, buyer)
        assert status == 201
        assert (body["entry_status"], body["position"]) == ("WAITING", None)

    # --- Release: shuffled once, pool (capacity 2) filled ------------------------------
    async def two_admitted() -> bool:
        states = [(await _me(system, eid, b))["entry_status"] for b in buyers]
        return states.count("ADMITTED") == 2

    await eventually(two_admitted, within=10)
    views = {b: await _me(system, eid, b) for b in buyers}
    admitted = [b for b in buyers if views[b]["entry_status"] == "ADMITTED"]
    waiting = sorted(
        (b for b in buyers if views[b]["entry_status"] == "WAITING"),
        key=lambda b: views[b]["position"],
    )
    assert [views[b]["position"] for b in waiting] == [1, 2, 3]
    cfg = await system.services.configs.load(eid)
    assert cfg is not None and cfg.status is QueueStatus.ADMITTING

    # Tokens validate exactly as Reservation will check them.
    jwks = (await system.client.get("/.well-known/jwks.json")).json()
    for buyer in admitted:
        validated = validate_admission_token(
            views[buyer]["admission_token"],
            jwks,
            user_id=buyer,
            event_id=eid,
            now=datetime.now(UTC),
        )
        assert validated.admission_id == views[buyer]["admission"]["admission_id"]

    # --- A late joiner goes to the back ------------------------------------------------
    late = user_id()
    status, body = await _join(system, eid, late)
    assert (status, body["position"]) == (201, 4)

    # --- Checkout confirms the first admitted buyer's order: next buyer in at once -------
    ordered, expiring = admitted
    system.checkout.publish(
        messages.order_confirmed(
            admission_id=views[ordered]["admission"]["admission_id"],
            user_id=ordered,
            event_id=eid,
        )
    )
    await eventually(_entry_is(system, eid, waiting[0], "ADMITTED"))
    assert (await _me(system, eid, ordered))["entry_status"] == "NONE"

    # --- The other admission hits its deadline: swept, next buyer admitted --------------
    expiring_id = views[expiring]["admission"]["admission_id"]
    await eventually(_admission_gone(system, expiring_id), within=ADMISSION_SECONDS + 5)
    await eventually(_entry_is(system, eid, waiting[1], "ADMITTED"))

    # --- Reservation reports the event sold out -----------------------------------------
    system.reservation.publish(messages.event_sold_out(eid))
    await eventually(_lobby_is(system, eid, "CLOSED"))

    # Whoever holds an admission keeps it; everyone else is out. (Which buyers are
    # admitted at this instant depends on deadlines racing the message, so assert
    # the rule, not the names.)
    after = {u: await _me(system, eid, u) for u in [*buyers, late]}
    assert {v["queue_status"] for v in after.values()} == {"CLOSED"}
    holders = sorted(
        (u for u, v in after.items() if v["entry_status"] == "ADMITTED"),
        key=lambda u: after[u]["admission"]["admitted_at"],
    )
    assert holders, "the buyer admitted moments ago should still hold an admission"
    assert {v["entry_status"] for u, v in after.items() if u not in holders} == {"NONE"}
    assert after[late]["entry_status"] == "NONE"  # last in line never got in
    status, body = await _join(system, eid, user_id())
    assert (status, body["error"]["code"]) == (410, "EVENT_SOLD_OUT")
    # Rejoin as the most recently admitted holder (the others' deadlines may land
    # any moment now): still admitted, same admission back.
    newest = holders[-1]
    status, body = await _join(system, eid, newest)
    assert status == 200, body
    assert body["admission"]["admission_id"] == after[newest]["admission"]["admission_id"]

    detail = (await system.client.get(f"/internal/v1/events/{eid}/queue")).json()
    assert (detail["queue_status"], detail["waiting_count"]) == ("CLOSED", 0)
    assert detail["closed_at"] is not None


async def test_cancelled_event(system: System) -> None:
    eid = event_id()
    release_at = await server_now(system.redis) + timedelta(seconds=3)
    system.events.publish(messages.event_published(eid, total_capacity=10, release_at=release_at))
    await eventually(_lobby_is(system, eid, "OPEN"))
    buyers = [user_id() for _ in range(2)]
    for buyer in buyers:
        await _join(system, eid, buyer)
    await eventually(_entry_is(system, eid, buyers[0], "ADMITTED"))
    await eventually(_entry_is(system, eid, buyers[1], "ADMITTED"))

    system.events.publish(messages.event_cancelled(eid, version=2))
    await eventually(_lobby_is(system, eid, "CANCELLED"))

    for buyer in buyers:
        body = await _me(system, eid, buyer)
        assert (body["queue_status"], body["entry_status"]) == ("CANCELLED", "NONE")
    status, body = await _join(system, eid, user_id())
    assert (status, body["error"]["code"]) == (410, "EVENT_CANCELLED")
