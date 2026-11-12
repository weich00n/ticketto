"""The user-service client: paging through recipients, and mapping its errors."""

import http.client
import urllib.request

import pytest

from app.event_client import EventClient
from app.user_client import HttpError, UserClient, UserNotFound, UserServiceUnavailable
from tests.unit.conftest import EVENT, sub


def _page(ids, next_after):
    return {
        "items": [{"user_id": i, "email": f"{i}@example.com", "name": i.upper()} for i in ids],
        "next_after": next_after,
    }


class CutOffResponse:
    """A response whose connection drops partway through the body."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        raise http.client.IncompleteRead(b'{"title":')


def test_event_details_survive_a_connection_cut_off_mid_response(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: CutOffResponse())
    assert EventClient("http://events.test:3001").details(EVENT) is None


def test_user_service_cut_off_mid_response_is_unavailable(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: CutOffResponse())
    with pytest.raises(UserServiceUnavailable):
        UserClient("http://users.test:8000").opted_in()


class FakeFetch:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls: list[str] = []

    def __call__(self, url: str):
        self.urls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_opted_in_pages_until_next_after_is_null():
    fetch = FakeFetch([_page(["a", "b"], "b"), _page(["c", "d"], "d"), _page(["e"], None)])
    client = UserClient("http://users.test:8000/", fetch=fetch, page_size=2)

    users = client.opted_in()

    assert [u.user_id for u in users] == ["a", "b", "c", "d", "e"]
    assert users[0].email == "a@example.com" and users[0].name == "A"
    assert fetch.urls == [
        "http://users.test:8000/internal/v1/users?notify_new_events=true&limit=2",
        "http://users.test:8000/internal/v1/users?notify_new_events=true&limit=2&after=b",
        "http://users.test:8000/internal/v1/users?notify_new_events=true&limit=2&after=d",
    ]


def test_buyers_filters_by_event():
    fetch = FakeFetch([_page(["a"], None)])
    client = UserClient("http://users.test:8000", fetch=fetch)
    assert [u.user_id for u in client.buyers(EVENT)] == ["a"]
    assert fetch.urls == [
        f"http://users.test:8000/internal/v1/users?purchased_event_id={EVENT}&limit=500"
    ]


def test_by_sub():
    fetch = FakeFetch([{"user_id": "u1", "email": "u1@example.com", "name": "U1", "role": "buyer"}])
    client = UserClient("http://users.test:8000", fetch=fetch)
    assert client.by_sub(sub(1)).email == "u1@example.com"
    assert fetch.urls == [f"http://users.test:8000/internal/v1/users/by-sub/{sub(1)}"]


def test_by_sub_404_is_user_not_found():
    client = UserClient("http://users.test:8000", fetch=FakeFetch([HttpError(404, "nope")]))
    with pytest.raises(UserNotFound):
        client.by_sub(sub(1))


@pytest.mark.parametrize(
    "error", [HttpError(503, "down"), HttpError(400, "bad"), OSError("refused")]
)
def test_other_failures_are_unavailable(error):
    client = UserClient("http://users.test:8000", fetch=FakeFetch([error]))
    with pytest.raises(UserServiceUnavailable):
        client.opted_in()


def test_malformed_response_is_unavailable():
    client = UserClient("http://users.test:8000", fetch=FakeFetch([{"items": "nope"}]))
    with pytest.raises(UserServiceUnavailable):
        client.opted_in()


def test_event_client_reads_title_and_start():
    fetch = FakeFetch(
        [{"event_id": EVENT, "title": "Autumn Live", "starts_at": "2026-11-20T12:00:00Z"}]
    )
    details = EventClient("http://events.test:3001", fetch=fetch).details(EVENT)
    assert details.title == "Autumn Live"
    assert details.starts_at.isoformat() == "2026-11-20T12:00:00"
    assert fetch.urls == [f"http://events.test:3001/api/v1/events/{EVENT}"]


@pytest.mark.parametrize("response", [HttpError(404, "no"), OSError("down"), {"title": ""}, []])
def test_event_client_never_raises(response):
    assert (
        EventClient("http://events.test:3001", fetch=FakeFetch([response])).details(EVENT) is None
    )


def test_event_client_without_url_returns_none():
    assert EventClient(None).details(EVENT) is None


def test_recipients_carry_cognito_sub_when_user_service_sends_it():
    page = {
        "items": [
            {"user_id": "a", "cognito_sub": "sub-a", "email": "a@example.com", "name": "A"},
            {"user_id": "b", "email": "b@example.com", "name": "B"},  # older user-service
        ],
        "next_after": None,
    }
    users = UserClient("http://users.test:8000", fetch=FakeFetch([page])).opted_in()
    assert [(u.user_id, u.cognito_sub) for u in users] == [("a", "sub-a"), ("b", None)]
