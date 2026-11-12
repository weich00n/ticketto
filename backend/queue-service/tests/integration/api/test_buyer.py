"""Buyer API end to end against real Redis and MySQL: every status and error
row of endpoints 1-4 in docs/Endpoint reference.md."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.redis import server_now
from app.core.status import QueueStatus
from app.core.timefmt import parse_iso, to_iso, to_ms
from app.queue import keys
from app.queue.config_repo import QueueConfigData
from app.queue.models import Admission
from tests.fakes.reservation_service import validate_admission_token
from tests.fixtures.factories import event_id, user_id
from tests.integration.api.conftest import Api

pytestmark = pytest.mark.integration

WAITING_PREQUEUE_KEYS = [
    "event_id",
    "queue_status",
    "entry_status",
    "position",
    "release_at",
    "poll_after_seconds",
]
WAITING_KEYS = ["event_id", "queue_status", "entry_status", "position", "poll_after_seconds"]
ADMITTED_KEYS = [
    "event_id",
    "queue_status",
    "entry_status",
    "admission",
    "admission_token",
    "poll_after_seconds",
]
NONE_KEYS = ["event_id", "queue_status", "entry_status", "poll_after_seconds"]


def _base(cfg: QueueConfigData) -> str:
    return f"/api/v1/events/{cfg.event_id}/queue"


async def _join(api: Api, cfg: QueueConfigData, user: str) -> Any:
    return await api.client.post(f"{_base(cfg)}/entries", headers=api.headers(user))


async def _me(api: Api, cfg: QueueConfigData, user: str) -> Any:
    return await api.client.get(f"{_base(cfg)}/me", headers=api.headers(user))


# --- 1. lobby --------------------------------------------------------------------------


async def test_lobby_info(api: Api) -> None:
    cfg = await api.event(release_in=timedelta(minutes=10))  # stored SCHEDULED, time says OPEN

    resp = await api.client.get(_base(cfg))

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "event_id": cfg.event_id,
        "queue_status": "OPEN",
        "queue_opens_at": to_iso(cfg.queue_opens_at),
        "release_at": to_iso(cfg.release_at),
        "sale_ends_at": None,
        "server_time": body["server_time"],
    }
    assert abs(parse_iso(body["server_time"]) - await server_now(api.redis)) < timedelta(seconds=2)


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("GET", ""), ("POST", "/entries"), ("GET", "/me"), ("DELETE", "/me")],
)
async def test_unknown_event_is_404_everywhere(api: Api, method: str, suffix: str) -> None:
    resp = await api.client.request(
        method, f"/api/v1/events/{event_id()}/queue{suffix}", headers=api.headers(user_id())
    )
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "EVENT_NOT_FOUND")


# --- 2/3. join and poll across the lifecycle --------------------------------------------


async def test_scheduled_event_refuses_joins(api: Api) -> None:
    cfg = await api.event(release_in=timedelta(hours=2))
    user = user_id()

    resp = await _join(api, cfg, user)

    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "QUEUE_NOT_OPEN"
    assert error["details"] == {"queue_opens_at": to_iso(cfg.queue_opens_at)}
    me = (await _me(api, cfg, user)).json()
    assert me == {
        "event_id": cfg.event_id,
        "queue_status": "SCHEDULED",
        "entry_status": "NONE",
        "poll_after_seconds": None,
    }


async def test_join_before_release_enters_prequeue(api: Api) -> None:
    cfg = await api.event(release_in=timedelta(minutes=10))
    user = user_id()

    created = await _join(api, cfg, user)
    again = await _join(api, cfg, user)
    polled = await _me(api, cfg, user)

    assert created.status_code == 201
    assert again.status_code == 200
    for resp in (created, again, polled):
        body = resp.json()
        assert list(body) == WAITING_PREQUEUE_KEYS
        assert (body["queue_status"], body["entry_status"], body["position"]) == (
            "OPEN",
            "WAITING",
            None,
        )
        assert body["release_at"] == to_iso(cfg.release_at)
        assert 30 <= body["poll_after_seconds"] <= 35


async def test_join_after_release_is_fifo_with_positions(api: Api) -> None:
    cfg = await api.admitting_event(capacity=1)
    first, second = user_id(), user_id()

    resp_first = await _join(api, cfg, first)
    resp_second = await _join(api, cfg, second)

    assert (resp_first.status_code, resp_second.status_code) == (201, 201)
    body = resp_second.json()
    assert list(body) == WAITING_KEYS
    assert (body["queue_status"], body["entry_status"], body["position"]) == (
        "ADMITTING",
        "WAITING",
        2,
    )
    assert resp_first.json()["position"] == 1
    assert resp_first.json()["poll_after_seconds"] == 3  # within capacity
    assert body["poll_after_seconds"] == 10  # within 10x capacity


async def test_admitted_buyer_gets_admission_and_identical_token(api: Api) -> None:
    user = user_id()
    cfg = await api.admitting_event(users=[user])
    (admission,) = (await api.services.queues.admit(cfg)).admitted

    first = await _me(api, cfg, user)
    second = await _me(api, cfg, user)
    rejoin = await _join(api, cfg, user)

    assert (first.status_code, second.status_code, rejoin.status_code) == (200, 200, 200)
    body = first.json()
    assert list(body) == ADMITTED_KEYS
    assert (body["entry_status"], body["poll_after_seconds"]) == ("ADMITTED", None)
    assert body["admission"] == admission.to_public()
    # Same admission, same token, no new timer: on every poll and on a repeat join.
    assert second.json() == body
    assert rejoin.json() == body

    validated = validate_admission_token(
        body["admission_token"],
        api.services.tokens.jwks(),
        user_id=user,
        event_id=cfg.event_id,
        now=datetime.now(UTC),
    )
    assert validated.admission_id == admission.admission_id


async def test_buyer_not_in_queue_sees_none(api: Api) -> None:
    cfg = await api.admitting_event()
    body = (await _me(api, cfg, user_id())).json()
    assert list(body) == NONE_KEYS
    assert (body["queue_status"], body["entry_status"]) == ("ADMITTING", "NONE")


async def test_rejoin_after_admission_ended_goes_to_back(api: Api) -> None:
    first, second = user_id(), user_id()
    cfg = await api.admitting_event(capacity=1, users=[first, second])
    (admission,) = (await api.services.queues.admit(cfg)).admitted
    await api.services.queues.release(admission.admission_id, first, cfg.event_id)

    resp = await _join(api, cfg, first)

    assert resp.status_code == 201
    assert (resp.json()["entry_status"], resp.json()["position"]) == ("WAITING", 2)


# --- selection deadline passed: requeued on the next poll or join ---------------------------


async def _lapse(api: Api, adm: Admission) -> None:
    """Move the selection deadline into the past (admission deadline untouched)."""
    now = await server_now(api.redis)
    key = keys.admission(adm.event_id, adm.admission_id)
    await api.redis.hset(key, "selection_expires_at", to_ms(now) - 1)


async def test_poll_after_selection_deadline_requeues_and_admits_next(api: Api) -> None:
    lapsed, nxt = user_id(), user_id()
    cfg = await api.admitting_event(capacity=1, users=[lapsed, nxt])
    (adm,) = (await api.services.queues.admit(cfg)).admitted
    await _lapse(api, adm)

    me = (await _me(api, cfg, lapsed)).json()

    assert (me["entry_status"], me["position"]) == ("WAITING", 1)
    assert "admission_token" not in me
    nxt_me = (await _me(api, cfg, nxt)).json()
    assert nxt_me["entry_status"] == "ADMITTED"  # admitted in the same request
    assert await api.redis.hget(keys.ADMISSION_INDEX, adm.admission_id) is None


async def test_join_after_selection_deadline_requeues_too(api: Api) -> None:
    lapsed, nxt = user_id(), user_id()
    cfg = await api.admitting_event(capacity=1, users=[lapsed, nxt])
    (adm,) = (await api.services.queues.admit(cfg)).admitted
    await _lapse(api, adm)

    resp = await _join(api, cfg, lapsed)

    assert resp.status_code == 200
    assert (resp.json()["entry_status"], resp.json()["position"]) == ("WAITING", 1)


async def test_lapsed_buyer_alone_in_queue_is_readmitted_with_new_deadlines(api: Api) -> None:
    user = user_id()
    cfg = await api.admitting_event(capacity=1, users=[user])
    (adm,) = (await api.services.queues.admit(cfg)).admitted
    await _lapse(api, adm)

    me = (await _me(api, cfg, user)).json()

    assert me["entry_status"] == "ADMITTED"
    assert me["admission"]["admission_id"] != adm.admission_id  # new admission, fresh timers


async def test_lapsed_buyer_on_sold_out_event_is_evicted_not_requeued(api: Api) -> None:
    user = user_id()
    cfg = await api.admitting_event(capacity=1, users=[user])
    (adm,) = (await api.services.queues.admit(cfg)).admitted
    await api.sell_out(cfg)
    await _lapse(api, adm)

    me = (await _me(api, cfg, user)).json()
    assert (me["queue_status"], me["entry_status"]) == ("CLOSED", "NONE")
    resp = await _join(api, cfg, user)
    assert (resp.status_code, resp.json()["error"]["code"]) == (410, "EVENT_SOLD_OUT")
    assert await api.redis.zcard(keys.active(cfg.event_id)) == 0


# --- sold out and cancelled ----------------------------------------------------------------


async def test_sold_out(api: Api) -> None:
    admitted, waiting = user_id(), user_id()
    cfg = await api.admitting_event(capacity=1, users=[admitted, waiting])
    (admission,) = (await api.services.queues.admit(cfg)).admitted
    await api.sell_out(cfg)

    # Admitted buyer keeps the admission (poll and repeat join alike).
    me = (await _me(api, cfg, admitted)).json()
    assert (me["queue_status"], me["entry_status"]) == ("CLOSED", "ADMITTED")
    assert me["admission"]["admission_id"] == admission.admission_id
    rejoin = await _join(api, cfg, admitted)
    assert rejoin.status_code == 200 and rejoin.json() == me

    # Waiting buyers were cleared and are told it's over.
    gone = (await _me(api, cfg, waiting)).json()
    assert gone == {
        "event_id": cfg.event_id,
        "queue_status": "CLOSED",
        "entry_status": "NONE",
        "poll_after_seconds": None,
    }
    for user in (waiting, user_id()):
        resp = await _join(api, cfg, user)
        assert (resp.status_code, resp.json()["error"]["code"]) == (410, "EVENT_SOLD_OUT")

    lobby = (await api.client.get(_base(cfg))).json()
    assert lobby["queue_status"] == "CLOSED"


async def test_cancelled(api: Api) -> None:
    admitted, waiting = user_id(), user_id()
    cfg = await api.admitting_event(capacity=1, users=[admitted, waiting])
    await api.services.queues.admit(cfg)
    await api.cancel(cfg)

    for user in (admitted, waiting):
        me = (await _me(api, cfg, user)).json()
        assert (me["queue_status"], me["entry_status"]) == ("CANCELLED", "NONE")
        resp = await _join(api, cfg, user)
        assert (resp.status_code, resp.json()["error"]["code"]) == (410, "EVENT_CANCELLED")


async def test_waiting_entry_reads_none_once_closed_even_before_cleanup(api: Api) -> None:
    user = user_id()
    cfg = await api.admitting_event(users=[user])
    await api.services.configs.set_status(cfg.event_id, QueueStatus.CLOSED)  # no close_event yet

    me = (await _me(api, cfg, user)).json()

    assert (me["queue_status"], me["entry_status"]) == ("CLOSED", "NONE")


# --- 4. leave ---------------------------------------------------------------------------


async def test_leave(api: Api) -> None:
    leaver, admitted = user_id(), user_id()
    cfg = await api.admitting_event(capacity=1, users=[admitted, leaver])
    await api.services.queues.admit(cfg)
    url = f"{_base(cfg)}/me"

    resp = await api.client.delete(url, headers=api.headers(leaver))
    assert (resp.status_code, resp.content) == (204, b"")
    assert (await _me(api, cfg, leaver)).json()["entry_status"] == "NONE"

    again = await api.client.delete(url, headers=api.headers(leaver))
    assert (again.status_code, again.json()["error"]["code"]) == (404, "NOT_IN_QUEUE")

    blocked = await api.client.delete(url, headers=api.headers(admitted))
    assert (blocked.status_code, blocked.json()["error"]["code"]) == (409, "ALREADY_ADMITTED")


async def test_leave_from_prequeue(api: Api) -> None:
    cfg = await api.event(release_in=timedelta(minutes=10))
    user = user_id()
    await _join(api, cfg, user)

    resp = await api.client.delete(f"{_base(cfg)}/me", headers=api.headers(user))

    assert resp.status_code == 204


# --- identity --------------------------------------------------------------------------------


async def test_identity_is_case_insensitive(api: Api) -> None:
    cfg = await api.event(release_in=timedelta(minutes=10))
    user = user_id()
    await _join(api, cfg, user.upper())

    body = (await _me(api, cfg, user)).json()

    assert body["entry_status"] == "WAITING"


async def test_missing_identity_is_401(api: Api) -> None:
    cfg = await api.event(release_in=timedelta(minutes=10))
    resp = await api.client.post(f"{_base(cfg)}/entries")
    assert (resp.status_code, resp.json()["error"]["code"]) == (401, "UNAUTHENTICATED")


# --- end of sale (sale_ends_at) ---------------------------------------------------------------


async def test_join_after_sale_end_is_sale_ended_and_writes_nothing(api: Api) -> None:
    waiting = user_id()
    cfg = await api.admitting_event(users=[waiting], sale_ends_in=timedelta(milliseconds=500))
    await asyncio.sleep(0.6)  # the sale ends; the admitter hasn't closed it yet

    newcomer = user_id()
    resp = await _join(api, cfg, newcomer)

    assert (resp.status_code, resp.json()["error"]["code"]) == (410, "SALE_ENDED")
    assert (await _me(api, cfg, newcomer)).json()["entry_status"] == "NONE"
    assert await api.redis.zcard(keys.waiting(cfg.event_id)) == 1  # only `waiting`
    me = (await _me(api, cfg, waiting)).json()
    assert (me["queue_status"], me["entry_status"]) == ("CLOSED", "NONE")


async def test_admitted_buyer_keeps_admission_after_sale_end(api: Api) -> None:
    user = user_id()
    cfg = await api.admitting_event(users=[user], sale_ends_in=timedelta(milliseconds=500))
    (admission,) = (await api.services.queues.admit(cfg)).admitted
    await asyncio.sleep(0.6)

    rejoin = await _join(api, cfg, user)

    assert rejoin.status_code == 200
    body = rejoin.json()
    assert (body["queue_status"], body["entry_status"]) == ("CLOSED", "ADMITTED")
    assert body["admission"]["admission_id"] == admission.admission_id


async def test_sold_out_before_sale_end_is_still_sold_out(api: Api) -> None:
    cfg = await api.admitting_event(sale_ends_in=timedelta(days=7))
    await api.sell_out(cfg)

    resp = await _join(api, cfg, user_id())

    assert (resp.status_code, resp.json()["error"]["code"]) == (410, "EVENT_SOLD_OUT")


async def test_lobby_shows_sale_end(api: Api) -> None:
    cfg = await api.event(release_in=timedelta(minutes=10), sale_ends_in=timedelta(days=7))

    body = (await api.client.get(_base(cfg))).json()

    assert cfg.sale_ends_at is not None
    assert body["sale_ends_at"] == to_iso(cfg.sale_ends_at)
