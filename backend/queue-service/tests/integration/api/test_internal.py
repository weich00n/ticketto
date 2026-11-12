"""Internal endpoints (5-9) against real Redis and MySQL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.status import QueueStatus
from app.core.timefmt import to_iso
from tests.fakes.reservation_service import validate_admission_token
from tests.fixtures.factories import DEFAULT_RELEASE, event_id, queue_config, user_id
from tests.integration.api.conftest import Api

pytestmark = pytest.mark.integration


# --- 5. list queues ----------------------------------------------------------------------


async def _collect(api: Api, path: str, **params: str | int) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    cursor = None
    while True:
        query: dict[str, str | int] = {**params, **({"cursor": cursor} if cursor else {})}
        resp = await api.client.get(path, params=query)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items += body["items"]
        cursor = body["next_cursor"]
        if cursor is None:
            return items


async def test_list_queues_pages_without_gaps_or_duplicates(api: Api) -> None:
    rows = [queue_config(release_at=DEFAULT_RELEASE + timedelta(hours=h % 3)) for h in range(7)]
    async with api.sessions.begin() as session:
        session.add_all(rows)

    items = await _collect(api, "/internal/v1/queues", limit=3)

    expected = sorted(rows, key=lambda r: (r.release_at, r.event_id))
    assert [i["event_id"] for i in items] == [r.event_id for r in expected]
    assert items[0] == {
        "event_id": expected[0].event_id,
        "queue_status": "SCHEDULED",
        "release_at": to_iso(expected[0].release_at),
    }


async def test_list_queues_filters_on_persisted_status(api: Api) -> None:
    closed = await api.event(release_in=timedelta(hours=1), status=QueueStatus.CLOSED)
    await api.event(release_in=timedelta(hours=1))

    resp = await api.client.get("/internal/v1/queues", params={"status": "CLOSED"})

    assert [i["event_id"] for i in resp.json()["items"]] == [closed.event_id]


async def test_list_queues_reports_effective_status(api: Api) -> None:
    """Persisted SCHEDULED, but the clock says the lobby is open."""
    cfg = await api.event(release_in=timedelta(minutes=5))
    (item,) = (await api.client.get("/internal/v1/queues")).json()["items"]
    assert (item["event_id"], item["queue_status"]) == (cfg.event_id, "OPEN")


async def test_list_queues_caps_limit_at_200(api: Api) -> None:
    async with api.sessions.begin() as session:
        session.add_all(queue_config() for _ in range(205))

    body = (await api.client.get("/internal/v1/queues", params={"limit": 500})).json()

    assert len(body["items"]) == 200
    assert body["next_cursor"] is not None


async def test_list_queues_rejects_bad_cursor(api: Api) -> None:
    resp = await api.client.get("/internal/v1/queues", params={"cursor": "garbage"})
    assert (resp.status_code, resp.json()["error"]["code"]) == (400, "INVALID_REQUEST")


# --- 6. queue detail ----------------------------------------------------------------------


async def test_queue_detail_with_live_counts(api: Api) -> None:
    users = [user_id() for _ in range(5)]
    cfg = await api.admitting_event(capacity=2, users=users)
    await api.services.queues.admit(cfg)

    body = (await api.client.get(f"/internal/v1/events/{cfg.event_id}/queue")).json()

    assert body == {
        "event_id": cfg.event_id,
        "queue_status": "ADMITTING",
        "event_version": 1,
        "total_capacity": 10,
        "active_pool_capacity": 2,
        "queue_opens_at": to_iso(cfg.queue_opens_at),
        "release_at": to_iso(cfg.release_at),
        "sale_ends_at": None,
        "closed_at": None,
        "waiting_count": 3,
        "active_count": 2,
    }


async def test_queue_detail_counts_prequeue_and_shows_closed_at(api: Api) -> None:
    cfg = await api.event(release_in=timedelta(minutes=5))
    for _ in range(3):
        await api.client.post(
            f"/api/v1/events/{cfg.event_id}/queue/entries", headers=api.headers(user_id())
        )
    detail = (await api.client.get(f"/internal/v1/events/{cfg.event_id}/queue")).json()
    assert (detail["queue_status"], detail["waiting_count"], detail["active_count"]) == (
        "OPEN",
        3,
        0,
    )

    sold_out_at = datetime(2026, 11, 20, 12, 7, 42, tzinfo=UTC)
    await api.services.configs.set_status(cfg.event_id, QueueStatus.CLOSED, closed_at=sold_out_at)
    await api.services.queues.close_event(cfg.event_id)
    detail = (await api.client.get(f"/internal/v1/events/{cfg.event_id}/queue")).json()
    assert (detail["queue_status"], detail["closed_at"], detail["waiting_count"]) == (
        "CLOSED",
        "2026-11-20T12:07:42Z",
        0,
    )


async def test_queue_detail_unknown_event(api: Api) -> None:
    resp = await api.client.get(f"/internal/v1/events/{event_id()}/queue")
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "EVENT_NOT_FOUND")


# --- 7/8. admissions ------------------------------------------------------------------------


async def test_list_admissions_oldest_first_across_pages(api: Api) -> None:
    users = [user_id() for _ in range(6)]
    cfg = await api.admitting_event(capacity=5, users=users)
    first_batch = (await api.services.queues.admit(cfg)).admitted
    gone = first_batch[0]
    await api.services.queues.release(gone.admission_id, gone.user_id, cfg.event_id)
    (late,) = (await api.services.queues.admit(cfg)).admitted

    items = await _collect(api, f"/internal/v1/events/{cfg.event_id}/queue/admissions", limit=2)

    earlier = sorted(first_batch[1:], key=lambda a: a.admission_id)
    assert items == [a.to_public() for a in [*earlier, late]]


async def test_list_admissions_unknown_event(api: Api) -> None:
    resp = await api.client.get(f"/internal/v1/events/{event_id()}/queue/admissions")
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "EVENT_NOT_FOUND")


async def test_get_admission_until_it_ends(api: Api) -> None:
    user = user_id()
    cfg = await api.admitting_event(users=[user])
    (adm,) = (await api.services.queues.admit(cfg)).admitted
    url = f"/internal/v1/admissions/{adm.admission_id}"

    found = await api.client.get(url)
    assert (found.status_code, found.json()) == (200, adm.to_public())
    assert "admission_token" not in found.json()

    await api.services.queues.release(adm.admission_id, user, cfg.event_id)
    gone = await api.client.get(url)
    assert (gone.status_code, gone.json()["error"]["code"]) == (404, "ADMISSION_NOT_FOUND")


# --- 9. JWKS ---------------------------------------------------------------------------------


async def test_token_from_buyer_api_validates_against_served_jwks(api: Api) -> None:
    """End to end, the way Reservation will do it: token from /queue/me, keys from JWKS."""
    user = user_id()
    cfg = await api.admitting_event(users=[user])
    await api.services.queues.admit(cfg)
    me = (
        await api.client.get(f"/api/v1/events/{cfg.event_id}/queue/me", headers=api.headers(user))
    ).json()

    jwks_resp = await api.client.get("/.well-known/jwks.json")

    assert jwks_resp.headers["cache-control"] == "public, max-age=300"
    validated = validate_admission_token(
        me["admission_token"],
        jwks_resp.json(),
        user_id=user,
        event_id=cfg.event_id,
        now=datetime.now(UTC),
    )
    assert validated.admission_id == me["admission"]["admission_id"]
