import re

from tests.unit.conftest import BUYER_SUB, OTHER_SUB, auth_headers, get_user_row

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


# ---- GET /api/v1/users/me ----


def test_get_me_returns_profile(client, make_user):
    make_user()
    res = client.get("/api/v1/users/me", headers=auth_headers())
    assert res.status_code == 200
    body = res.json()
    assert body["user_id"] == "8f14e45f-ceea-4b6a-9a3b-1f2d3c4e5f60"
    assert body["email"] == "alice@example.com"
    assert body["name"] == "Alice Tan"
    assert body["role"] == "buyer"
    assert ISO_Z.match(body["created_at"]) and ISO_Z.match(body["updated_at"])
    assert body["correlation_id"] == res.headers["X-Correlation-Id"]
    assert "cognito_sub" not in body


def test_get_me_not_found(client):
    res = client.get("/api/v1/users/me", headers=auth_headers())
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "USER_NOT_FOUND"


def test_get_me_missing_headers(client, make_user):
    make_user()
    assert client.get("/api/v1/users/me").status_code == 401
    res = client.get("/api/v1/users/me", headers={"X-User-Sub": BUYER_SUB})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHENTICATED"


def test_get_me_only_sees_own_profile(client, make_user):
    make_user()
    res = client.get("/api/v1/users/me", headers=auth_headers(sub=OTHER_SUB))
    assert res.status_code == 404


# ---- POST /api/v1/users/me ----


def test_post_me_creates_profile_from_cognito(client, session_factory):
    res = client.post("/api/v1/users/me", headers=auth_headers())
    assert res.status_code == 201
    body = res.json()
    assert body["email"] == "alice@example.com"
    assert body["name"] == "Alice Tan"
    assert body["role"] == "buyer"
    row = get_user_row(session_factory)
    assert row is not None and row.user_id == body["user_id"]


def test_post_me_ignores_body(client):
    res = client.post(
        "/api/v1/users/me",
        headers=auth_headers(),
        json={"email": "evil@example.com", "name": "Mallory", "role": "organiser"},
    )
    assert res.status_code == 201
    assert res.json()["email"] == "alice@example.com"
    assert res.json()["role"] == "buyer"


def test_post_me_is_idempotent(client):
    first = client.post("/api/v1/users/me", headers=auth_headers())
    second = client.post("/api/v1/users/me", headers=auth_headers())
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["user_id"] == first.json()["user_id"]


def test_post_me_existing_profile_skips_cognito(client, cognito, make_user):
    make_user()
    cognito.fail = True
    res = client.post("/api/v1/users/me", headers=auth_headers())
    assert res.status_code == 200


def test_post_me_handles_concurrent_insert(client, cognito, make_user):
    # Simulate another request inserting the row after our lookup but before our insert.
    cognito.before_get_user = lambda: make_user(user_id="99999999-9999-4999-8999-999999999999")
    res = client.post("/api/v1/users/me", headers=auth_headers())
    assert res.status_code == 200
    assert res.json()["user_id"] == "99999999-9999-4999-8999-999999999999"


def test_post_me_cognito_unavailable(client, cognito, session_factory):
    cognito.fail = True
    res = client.post("/api/v1/users/me", headers=auth_headers())
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "COGNITO_UNAVAILABLE"
    assert get_user_row(session_factory) is None


def test_post_me_cognito_user_missing(client):
    res = client.post("/api/v1/users/me", headers=auth_headers(sub=OTHER_SUB))
    assert res.status_code == 401


def test_post_me_rejects_sub_mismatch(client, cognito):
    cognito.users[OTHER_SUB] = {"sub": BUYER_SUB, "email": "x@example.com", "name": "X"}
    res = client.post("/api/v1/users/me", headers=auth_headers(sub=OTHER_SUB))
    assert res.status_code == 401


def test_post_me_name_falls_back_to_email(client, cognito):
    del cognito.users[BUYER_SUB]["name"]
    res = client.post("/api/v1/users/me", headers=auth_headers())
    assert res.status_code == 201
    assert res.json()["name"] == "alice"


# ---- PATCH /api/v1/users/me ----


def test_patch_me_updates_name_in_db_and_cognito(client, cognito, make_user, session_factory):
    make_user()
    res = client.patch("/api/v1/users/me", headers=auth_headers(), json={"name": "  Alice Lim  "})
    assert res.status_code == 200
    assert res.json()["name"] == "Alice Lim"
    assert get_user_row(session_factory).name == "Alice Lim"
    assert cognito.users[BUYER_SUB]["name"] == "Alice Lim"


def test_patch_me_rejects_unknown_fields(client, make_user, session_factory):
    make_user()
    for field, value in [("role", "organiser"), ("email", "x@example.com"), ("user_id", "x")]:
        res = client.patch(
            "/api/v1/users/me", headers=auth_headers(), json={"name": "A", field: value}
        )
        assert res.status_code == 400, field
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"
    row = get_user_row(session_factory)
    assert row.role == "buyer" and row.name == "Alice Tan"


def test_patch_me_rejects_invalid_name(client, make_user):
    make_user()
    for payload in [{"name": ""}, {"name": "   "}, {"name": "x" * 256}, {}, {"name": 5}]:
        res = client.patch("/api/v1/users/me", headers=auth_headers(), json=payload)
        assert res.status_code == 400, payload


def test_patch_me_not_found(client):
    res = client.patch("/api/v1/users/me", headers=auth_headers(), json={"name": "A"})
    assert res.status_code == 404


def test_patch_me_cognito_failure_leaves_db_unchanged(client, cognito, make_user, session_factory):
    make_user()
    cognito.fail = True
    res = client.patch("/api/v1/users/me", headers=auth_headers(), json={"name": "Alice Lim"})
    assert res.status_code == 503
    assert get_user_row(session_factory).name == "Alice Tan"


def test_patch_me_same_name_skips_cognito(client, cognito, make_user):
    make_user()
    cognito.fail = True
    res = client.patch("/api/v1/users/me", headers=auth_headers(), json={"name": "Alice Tan"})
    assert res.status_code == 200


# ---- POST /api/v1/users/me/organiser ----


def test_become_organiser(client, cognito, make_user, session_factory):
    make_user()
    res = client.post("/api/v1/users/me/organiser", headers=auth_headers())
    assert res.status_code == 200
    body = res.json()
    assert body["role"] == "organiser"
    assert body["token_refresh_required"] is True
    assert "organiser" in cognito.groups[BUYER_SUB]
    assert get_user_row(session_factory).role == "organiser"


def test_become_organiser_already_in_token(client, make_user):
    make_user(role="organiser")
    res = client.post(
        "/api/v1/users/me/organiser", headers=auth_headers(groups="[organiser]"), json={}
    )
    assert res.status_code == 200
    assert res.json()["token_refresh_required"] is False


def test_become_organiser_db_set_but_token_stale(client, make_user):
    make_user(role="organiser")
    res = client.post("/api/v1/users/me/organiser", headers=auth_headers(groups="[]"))
    assert res.json()["token_refresh_required"] is True


def test_become_organiser_cognito_failure_leaves_db_unchanged(
    client, cognito, make_user, session_factory
):
    make_user()
    cognito.fail = True
    res = client.post("/api/v1/users/me/organiser", headers=auth_headers())
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "COGNITO_UNAVAILABLE"
    assert get_user_row(session_factory).role == "buyer"


def test_become_organiser_not_found(client):
    res = client.post("/api/v1/users/me/organiser", headers=auth_headers())
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "USER_NOT_FOUND"


def test_become_organiser_missing_headers(client):
    assert client.post("/api/v1/users/me/organiser").status_code == 401
