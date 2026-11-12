"""Internal lookups for notification-service: who to email, and a user by Cognito sub."""

import uuid

E1 = "a14ec682-62f5-46db-b94a-a8d95435e274"
E2 = "b05e8625-6d43-4252-ad2c-bda3222a769b"
E3 = "c41a7e2d-5f86-4b09-9e3c-7d2b1a0f6e58"


def _uid(n: int) -> str:
    # Sorted by number, so pages come back in a known order.
    return f"00000000-0000-4000-8000-{n:012d}"


def _sub(n: int) -> str:
    return f"10000000-0000-4000-8000-{n:012d}"


def _people(make_user, *specs: dict) -> None:
    for n, spec in enumerate(specs, start=1):
        make_user(sub=_sub(n), user_id=_uid(n), email=f"u{n}@example.com", name=f"U{n}", **spec)


def _ids(body: dict) -> list[str]:
    return [item["user_id"] for item in body["items"]]


def test_lists_only_opted_in_users(client, make_user):
    _people(make_user, {}, {"notify_new_events": False}, {"notify_new_events": True})
    res = client.get("/internal/v1/users", params={"notify_new_events": "true"})
    assert res.status_code == 200
    body = res.json()
    assert _ids(body) == [_uid(1), _uid(3)]
    # cognito_sub lets notification-service match a signed-in user's token to their rows.
    assert body["items"][0] == {
        "user_id": _uid(1),
        "cognito_sub": _sub(1),
        "email": "u1@example.com",
        "name": "U1",
    }
    assert body["next_after"] is None


def test_lists_only_buyers_of_the_event(client, make_user):
    _people(
        make_user,
        {"purchased_event_ids": E1},
        {"purchased_event_ids": E2},
        {"purchased_event_ids": f"{E2},{E1},{E3}"},
        {},
    )
    res = client.get("/internal/v1/users", params={"purchased_event_id": E1})
    assert res.status_code == 200
    assert _ids(res.json()) == [_uid(1), _uid(3)]
    assert [item["cognito_sub"] for item in res.json()["items"]] == [_sub(1), _sub(3)]


def test_buyers_include_users_who_opted_out(client, make_user):
    _people(make_user, {"purchased_event_ids": E1, "notify_new_events": False})
    res = client.get("/internal/v1/users", params={"purchased_event_id": E1})
    assert _ids(res.json()) == [_uid(1)]


def test_purchase_match_is_exact(client, make_user):
    prefix = E1[:-1]  # an ID that is a prefix of E1 must not match, and vice versa
    longer = E1 + "0"
    _people(
        make_user,
        {"purchased_event_ids": f"{E2},{E1}"},  # last item
        {"purchased_event_ids": f"{E1},{E2}"},  # first item
        {"purchased_event_ids": f"{E3},{E1},{E2}"},  # middle item
        {"purchased_event_ids": f"{prefix}"},
        {"purchased_event_ids": f"{longer},{E2}"},
    )
    res = client.get("/internal/v1/users", params={"purchased_event_id": E1})
    assert _ids(res.json()) == [_uid(1), _uid(2), _uid(3)]


def test_purchase_filter_accepts_uppercase_ids(client, make_user):
    _people(make_user, {"purchased_event_ids": E1})
    res = client.get("/internal/v1/users", params={"purchased_event_id": E1.upper()})
    assert _ids(res.json()) == [_uid(1)]


def test_pages_with_after_cursor(client, make_user):
    _people(make_user, {}, {}, {}, {}, {})
    seen: list[str] = []
    after = None
    sizes = []
    for _ in range(5):
        params = {"notify_new_events": "true", "limit": 2}
        if after:
            params["after"] = after
        body = client.get("/internal/v1/users", params=params).json()
        sizes.append(len(body["items"]))
        seen += _ids(body)
        after = body["next_after"]
        if after is None:
            break
    assert sizes == [2, 2, 1]
    assert seen == [_uid(n) for n in range(1, 6)]


def test_full_last_page_still_ends(client, make_user):
    _people(make_user, {}, {})
    first = client.get("/internal/v1/users", params={"notify_new_events": "true", "limit": 2})
    assert first.json()["next_after"] == _uid(2)
    second = client.get(
        "/internal/v1/users",
        params={"notify_new_events": "true", "limit": 2, "after": _uid(2)},
    )
    assert second.json() == {"items": [], "next_after": None}


def test_rejects_bad_list_queries(client):
    bad = [
        {},
        {"notify_new_events": "true", "purchased_event_id": E1},
        {"notify_new_events": "false"},  # only opted-in users can be listed
        {"purchased_event_id": "not-a-uuid"},
        {"notify_new_events": "true", "limit": 0},
        {"notify_new_events": "true", "limit": 1001},
        {"notify_new_events": "true", "after": "nope"},
    ]
    for params in bad:
        res = client.get("/internal/v1/users", params=params)
        assert res.status_code == 400, params
        assert res.json()["error"]["code"] == "VALIDATION_ERROR", params


def test_get_user_by_sub(client, make_user):
    _people(make_user, {"notify_new_events": False})
    res = client.get(f"/internal/v1/users/by-sub/{_sub(1)}")
    assert res.status_code == 200
    body = res.json()
    assert body["user_id"] == _uid(1)
    assert body["email"] == "u1@example.com"
    assert body["notify_new_events"] is False


def test_get_user_by_unknown_sub(client):
    res = client.get(f"/internal/v1/users/by-sub/{uuid.uuid4()}")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "USER_NOT_FOUND"


def test_get_user_by_id_still_works(client, make_user):
    _people(make_user, {})
    assert client.get(f"/internal/v1/users/{_uid(1)}").status_code == 200
