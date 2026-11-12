"""The notifications API for signed-in users (PLAN.md tests 1-8, rollout-plan.md tests 9-13).

API Gateway has already checked the Cognito token, and sets X-User-Sub from it, replacing
anything the client sent. The API only ever returns that user's own rows."""

import logging
from datetime import datetime, timedelta

import pytest
from alembic import command
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.migrations import alembic_config
from app.models import (
    KIND_EVENT_CANCELLED,
    KIND_EVENT_PUBLISHED,
    KIND_ORDER_REFUNDED,
    KIND_REFUND_FAILED,
    KIND_TICKET_ISSUED,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SENT,
    Notification,
)
from tests.unit.conftest import EVENT, notifications, sub, ticket_context

ME, SOMEONE_ELSE = sub(1), sub(2)
T0 = datetime(2026, 10, 28, 12, 0, 0)

CONTEXTS = {
    KIND_EVENT_PUBLISHED: {"title": "Autumn Live", "starts_at": "2026-11-20T12:00:00Z"},
    KIND_EVENT_CANCELLED: {
        "title": "Autumn Live",
        "starts_at": "2026-11-20T12:00:00Z",
        "reason": "Venue unavailable.",
    },
    KIND_ORDER_REFUNDED: {
        "title": "Autumn Live",
        "amount": "SGD 88.00",
        "reason": "event_cancelled",
        "order_id": "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b",
    },
    KIND_REFUND_FAILED: {
        "title": "Autumn Live",
        "amount": "SGD 88.00",
        "reason": "event_cancelled",
        "order_id": "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b",
    },
    KIND_TICKET_ISSUED: ticket_context(tickets=1),
}


def call(  # pylint: disable=too-many-arguments
    session_factory, method: str, path: str, *, sub_claim: str | None = ME, query=None
) -> tuple[int, dict | None]:
    """A real HTTP request, with the X-User-Sub header the gateway sets from the verified token."""
    response = send(session_factory, method, path, sub_claim=sub_claim, query=query)
    return response.status_code, response.json() if response.content else None


def send(session_factory, method: str, path: str, *, sub_claim: str | None = ME, query=None):
    headers = {"X-Correlation-Id": "corr-1"}
    if sub_claim is not None:
        headers["X-User-Sub"] = sub_claim
    client = TestClient(create_app(session_factory), raise_server_exceptions=False)
    return client.request(method, path, params=query, headers=headers)


_counter = iter(range(1, 10_000))


def add(  # pylint: disable=too-many-arguments  # keyword-only test data
    session_factory,
    owner: str | None = ME,
    *,
    kind: str = KIND_EVENT_PUBLISHED,
    status: str = STATUS_SENT,
    minutes: int = 0,
    read: bool = False,
) -> int:
    """Store a notification for `owner` (a Cognito sub), created `minutes` after T0."""
    n = next(_counter)
    with session_factory() as session, session.begin():
        row = Notification(
            kind=kind,
            dedupe_key=f"test:{n}",
            source_message_id=f"msg-{n}",
            event_id=EVENT,
            user_id=f"user-for-{owner}",
            cognito_sub=owner,
            recipient="someone@example.com",
            context={**CONTEXTS[kind], "name": "Sophia"},
            status=status,
            attempts=1,
            created_at=T0 + timedelta(minutes=minutes),
            read_at=T0 + timedelta(minutes=minutes + 1) if read else None,
        )
        session.add(row)
        session.flush()
        return row.id


# ---------- 1. only the caller's rows, newest first ----------


def test_lists_only_my_notifications_newest_first(session_factory):
    older = add(session_factory, minutes=1)
    newer = add(session_factory, minutes=5)
    add(session_factory, SOMEONE_ELSE, minutes=3)
    add(session_factory, None, minutes=4)  # an old row stored before subs were kept

    status, body = call(session_factory, "GET", "/api/v1/notifications")

    assert status == 200
    assert [item["notification_id"] for item in body["items"]] == [newer, older]
    assert body["total_items"] == 2


def test_sub_is_matched_case_insensitively(session_factory):
    add(session_factory)
    status, body = call(session_factory, "GET", "/api/v1/notifications", sub_claim=ME.upper())
    assert status == 200 and body["total_items"] == 1


# ---------- 2. paging ----------


def test_pages_use_the_frontends_page_shape(session_factory):
    ids = [add(session_factory, minutes=m) for m in range(5)]  # ids[4] is the newest

    _, first = call(
        session_factory, "GET", "/api/v1/notifications", query={"page": "1", "page_size": "2"}
    )
    _, last = call(
        session_factory, "GET", "/api/v1/notifications", query={"page": "3", "page_size": "2"}
    )

    assert {k: first[k] for k in ("page", "page_size", "total_items")} == {
        "page": 1,
        "page_size": 2,
        "total_items": 5,
    }
    assert [i["notification_id"] for i in first["items"]] == [ids[4], ids[3]]
    assert [i["notification_id"] for i in last["items"]] == [ids[0]]


def test_default_page_size_is_20(session_factory):
    for m in range(25):
        add(session_factory, minutes=m)
    _, body = call(session_factory, "GET", "/api/v1/notifications")
    assert body["page"] == 1 and body["page_size"] == 20 and len(body["items"]) == 20


@pytest.mark.parametrize(
    "query",
    [{"page": "0"}, {"page": "x"}, {"page_size": "0"}, {"page_size": "101"}, {"page_size": "x"}],
)
def test_bad_paging_is_400(session_factory, query):
    status, body = call(session_factory, "GET", "/api/v1/notifications", query=query)
    assert status == 400
    assert body["error"]["code"] == "INVALID_REQUEST"


# ---------- 3. which emails show ----------


def test_withdrawn_emails_are_hidden_but_failed_and_pending_show(session_factory):
    sent = add(session_factory, status=STATUS_SENT, minutes=1)
    failed = add(session_factory, status=STATUS_FAILED, minutes=2)
    pending = add(session_factory, status=STATUS_PENDING, minutes=3)
    add(session_factory, status=STATUS_CANCELLED, minutes=4)  # never sent: event was off

    _, body = call(session_factory, "GET", "/api/v1/notifications")

    assert [(i["notification_id"], i["email_status"]) for i in body["items"]] == [
        (pending, STATUS_PENDING),
        (failed, STATUS_FAILED),
        (sent, STATUS_SENT),
    ]


# ---------- 4. what each item says ----------


@pytest.mark.parametrize(
    ("kind", "title", "preview"),
    [
        (
            KIND_EVENT_PUBLISHED,
            "New on Ticketto: Autumn Live",
            "A new event is on Ticketto: Autumn Live.",
        ),
        (
            KIND_EVENT_CANCELLED,
            "Cancelled: Autumn Live",
            "Autumn Live on Fri 20 Nov 2026, 08:00 PM SGT has been cancelled by the organiser.",
        ),
        (
            KIND_ORDER_REFUNDED,
            "Your refund of SGD 88.00 for Autumn Live",
            "We've refunded SGD 88.00 for Autumn Live because the event was cancelled.",
        ),
        (
            KIND_REFUND_FAILED,
            "We couldn't complete your refund for Autumn Live",
            "We tried to refund SGD 88.00 for Autumn Live, but the refund didn't go through, "
            "so you haven't received this money yet.",
        ),
        (
            KIND_TICKET_ISSUED,
            "Your 1 ticket for Autumn Live",
            "Your ticket for Autumn Live is confirmed.",
        ),
    ],
)
def test_item_has_the_emails_subject_and_first_line(session_factory, kind, title, preview):
    nid = add(session_factory, kind=kind)

    _, body = call(session_factory, "GET", "/api/v1/notifications")

    [item] = body["items"]
    assert item == {
        "notification_id": nid,
        "kind": kind,
        "title": title,
        "preview": preview,
        "event_id": EVENT,
        "order_id": CONTEXTS[kind].get("order_id"),
        "created_at": "2026-10-28T12:00:00Z",
        "email_status": STATUS_SENT,
        "read_at": None,
    }


@pytest.mark.parametrize("status", [STATUS_PENDING, STATUS_SENT, STATUS_FAILED])
def test_a_ticket_notification_never_returns_the_qr_text(session_factory, status):
    # A PENDING row still holds it, waiting to be sent. Buyers see the code on /tickets.
    add(session_factory, kind=KIND_TICKET_ISSUED, status=status)
    response = send(session_factory, "GET", "/api/v1/notifications")
    assert response.json()["total_items"] == 1
    assert "TKT1." not in response.text


# ---------- 5. unread count ----------


def test_unread_count_counts_only_my_unread_visible_rows(session_factory):
    add(session_factory)
    add(session_factory, status=STATUS_FAILED)
    add(session_factory, read=True)
    add(session_factory, status=STATUS_CANCELLED)
    add(session_factory, SOMEONE_ELSE)

    status, body = call(session_factory, "GET", "/api/v1/notifications/unread-count")

    assert status == 200 and body == {"unread": 2}


# ---------- 6. marking one as read ----------


def test_marking_my_notification_read(session_factory):
    nid = add(session_factory)

    status, body = call(session_factory, "POST", f"/api/v1/notifications/{nid}/read")

    assert status == 204 and body is None
    [row] = notifications(session_factory)
    assert row.read_at is not None
    _, listed = call(session_factory, "GET", "/api/v1/notifications")
    assert listed["items"][0]["read_at"] is not None


def test_marking_twice_keeps_the_first_time(session_factory):
    nid = add(session_factory, read=True)
    [before] = notifications(session_factory)

    status, _ = call(session_factory, "POST", f"/api/v1/notifications/{nid}/read")

    [after] = notifications(session_factory)
    assert status == 204 and after.read_at == before.read_at


@pytest.mark.parametrize("owner", [SOMEONE_ELSE, None])
def test_someone_elses_notification_is_404(session_factory, owner):
    nid = add(session_factory, owner)

    status, body = call(session_factory, "POST", f"/api/v1/notifications/{nid}/read")

    assert status == 404 and body["error"]["code"] == "NOT_FOUND"
    [row] = notifications(session_factory)
    assert row.read_at is None


def test_missing_or_withdrawn_notification_is_404(session_factory):
    withdrawn = add(session_factory, status=STATUS_CANCELLED)
    for nid in (withdrawn, 999_999):
        status, _ = call(session_factory, "POST", f"/api/v1/notifications/{nid}/read")
        assert status == 404


def test_non_numeric_id_is_400(session_factory):
    status, body = call(session_factory, "POST", "/api/v1/notifications/abc/read")
    assert status == 400 and body["error"]["code"] == "INVALID_REQUEST"


# ---------- 7. marking all as read ----------


def test_read_all_marks_only_mine(session_factory):
    add(session_factory)
    add(session_factory, status=STATUS_FAILED)
    add(session_factory, SOMEONE_ELSE)

    status, _ = call(session_factory, "POST", "/api/v1/notifications/read-all")

    assert status == 204
    by_owner = {(r.cognito_sub, r.read_at is not None) for r in notifications(session_factory)}
    assert by_owner == {(ME, True), (SOMEONE_ELSE, False)}
    _, count = call(session_factory, "GET", "/api/v1/notifications/unread-count")
    assert count == {"unread": 0}


# ---------- 8. requests that aren't allowed ----------


def test_no_sub_claim_is_401(session_factory):
    add(session_factory)
    status, body = call(session_factory, "GET", "/api/v1/notifications", sub_claim=None)
    assert status == 401 and body["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/notifications/123"),
        ("DELETE", "/api/v1/notifications"),
        ("GET", "/api/v1/notifications/read-all"),
        ("GET", "/api/v1/other"),
    ],
)
def test_unknown_route_is_404(session_factory, method, path):
    status, body = call(session_factory, method, path)
    assert status == 404 and body["error"]["code"] == "NOT_FOUND"


def test_errors_carry_the_correlation_id(session_factory):
    _, body = call(session_factory, "GET", "/api/v1/notifications", sub_claim=None)
    assert body["correlation_id"] == "corr-1"


def test_responses_are_json_and_never_cached(session_factory):
    response = send(session_factory, "GET", "/api/v1/notifications")
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"


# ---------- rollout-plan.md tests 10-13 ----------


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_header_is_401(session_factory, value):
    add(session_factory)
    status, body = call(session_factory, "GET", "/api/v1/notifications", sub_claim=value)
    assert status == 401 and body["error"]["code"] == "UNAUTHORIZED"
    assert body["correlation_id"] == "corr-1"


@pytest.mark.parametrize(
    "route",
    [
        ("GET", "/api/v1/notifications"),
        ("GET", "/api/v1/notifications/unread-count"),
        ("POST", "/api/v1/notifications/read-all"),
        ("POST", "/api/v1/notifications/1/read"),
    ],
)
def test_every_route_needs_the_header(session_factory, route):
    status, _ = call(session_factory, *route, sub_claim=None)
    assert status == 401


def test_bad_paging_isnt_fastapis_422(session_factory):
    status, body = call(session_factory, "GET", "/api/v1/notifications", query={"page": "x"})
    assert status == 400
    assert set(body) == {"error", "correlation_id"}
    assert body["error"] == {"code": "INVALID_REQUEST", "message": "page must be a whole number."}


def test_an_unknown_route_has_the_teams_error_shape(session_factory):
    status, body = call(session_factory, "GET", "/api/v1/other")
    assert status == 404
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["correlation_id"] == "corr-1"
    assert "detail" not in body


def test_a_trailing_slash_doesnt_redirect(session_factory):
    response = send(session_factory, "GET", "/api/v1/notifications/")
    assert response.status_code == 404  # the gateway only routes the exact paths


def test_health_needs_no_header_and_no_database():
    def no_database():
        raise AssertionError("/health must not touch the database")

    client = TestClient(create_app(no_database))
    response = client.get("/health")
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_the_correlation_id_is_echoed_on_success(session_factory):
    response = send(session_factory, "GET", "/api/v1/notifications")
    assert response.headers["x-correlation-id"] == "corr-1"


def test_an_unsafe_correlation_id_is_replaced(session_factory):
    client = TestClient(create_app(session_factory))
    headers = {"X-User-Sub": ME, "X-Correlation-Id": "bad id\twith\tspaces"}
    response = client.get("/api/v1/notifications", headers=headers)
    assert response.status_code == 200
    assert "bad" not in response.headers["x-correlation-id"]


def test_a_204_has_no_body_but_keeps_the_headers(session_factory):
    response = send(session_factory, "POST", "/api/v1/notifications/read-all")
    assert response.status_code == 204 and response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-correlation-id"] == "corr-1"


def test_each_request_is_logged_with_who_asked(session_factory, caplog):
    caplog.set_level(logging.INFO)
    send(session_factory, "GET", "/api/v1/notifications")
    [record] = [r for r in caplog.records if r.getMessage() == "request completed"]
    assert (record.method, record.path, record.status) == ("GET", "/api/v1/notifications", 200)
    assert record.user_sub == ME


def test_an_unexpected_error_is_a_500_that_leaks_nothing(caplog):
    def broken():
        raise RuntimeError("database unreachable at db-ticketing")

    response = TestClient(create_app(broken), raise_server_exceptions=False).get(
        "/api/v1/notifications",
        headers={"X-User-Sub": ME, "X-Correlation-Id": "corr-1"},
    )
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL"
    assert body["correlation_id"] == "corr-1"
    assert "db-ticketing" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "request failed" in caplog.text


def test_the_api_needs_only_the_database(monkeypatch, tmp_path):
    url = f"sqlite:///{tmp_path / 'api.db'}"
    command.upgrade(
        alembic_config(
            Settings(database_url=url, rabbitmq_url="", user_service_url="", require_runtime=False)
        ),
        "head",
    )
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("RABBITMQ_URL", raising=False)
    monkeypatch.delenv("USER_SERVICE_URL", raising=False)

    with TestClient(create_app()) as client:  # the real startup, as uvicorn runs it
        response = client.get("/api/v1/notifications/unread-count", headers={"X-User-Sub": ME})

    assert (response.status_code, response.json()) == (200, {"unread": 0})
