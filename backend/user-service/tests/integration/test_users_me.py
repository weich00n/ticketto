"""/api/v1/users/me against the real service, MySQL and cognito-local."""

import re
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_first_login_creates_profile_from_cognito(api, user, db_row):
    assert api.get("/api/v1/users/me", headers=user.headers()).status_code == 404

    res = api.post("/api/v1/users/me", headers=user.headers())
    assert res.status_code == 201
    body = res.json()
    assert body["email"] == user.email  # read from Cognito via AdminGetUser
    assert body["name"] == user.name
    assert body["role"] == "buyer"
    assert ISO_Z.match(body["created_at"]) and ISO_Z.match(body["updated_at"])

    row = db_row(user.sub)
    assert row["user_id"] == body["user_id"]
    assert row["email"] == user.email and row["role"] == "buyer"

    got = api.get("/api/v1/users/me", headers=user.headers())
    assert got.status_code == 200 and got.json()["user_id"] == body["user_id"]


def test_first_login_is_idempotent(api, user):
    first = api.post("/api/v1/users/me", headers=user.headers())
    second = api.post("/api/v1/users/me", headers=user.headers())
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["user_id"] == second.json()["user_id"]


def test_post_body_cannot_override_cognito_data(api, user, db_row):
    res = api.post(
        "/api/v1/users/me",
        headers=user.headers(),
        json={"email": "evil@test.com", "name": "Mallory", "role": "organiser"},
    )
    assert res.status_code == 201
    row = db_row(user.sub)
    assert (row["email"], row["name"], row["role"]) == (user.email, user.name, "buyer")


def test_concurrent_first_logins_create_one_row(api, user, db_engine):
    """Real MySQL UNIQUE(cognito_sub) race: many simultaneous first logins, one profile."""
    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(
            pool.map(lambda _: api.post("/api/v1/users/me", headers=user.headers()), range(10))
        )
    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(201) == 1, statuses
    assert statuses.count(200) == 9, statuses
    assert len({r.json()["user_id"] for r in responses}) == 1

    with db_engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM users WHERE cognito_sub = :s"), {"s": user.sub}
        ).scalar()
    assert count == 1


def test_user_deleted_from_cognito_gets_401(api, user, cognito, pool_id, db_row):
    cognito.admin_delete_user(UserPoolId=pool_id, Username=user.email)
    res = api.post("/api/v1/users/me", headers=user.headers())
    assert res.status_code == 401
    assert db_row(user.sub) is None


def test_patch_updates_mysql_and_cognito(api, user, db_row, cognito_name):
    created = api.post("/api/v1/users/me", headers=user.headers()).json()
    time.sleep(1.1)  # TIMESTAMP has 1-second precision

    res = api.patch("/api/v1/users/me", headers=user.headers(), json={"name": "  Renamed User "})
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "Renamed User"
    assert body["updated_at"] > created["updated_at"]
    assert body["created_at"] == created["created_at"]

    assert db_row(user.sub)["name"] == "Renamed User"
    assert cognito_name(user) == "Renamed User"


@pytest.mark.parametrize(
    "extra", [{"role": "organiser"}, {"email": "x@test.com"}, {"user_id": "x"}]
)
def test_patch_rejects_protected_fields(api, user, db_row, extra):
    api.post("/api/v1/users/me", headers=user.headers())
    res = api.patch("/api/v1/users/me", headers=user.headers(), json={"name": "X", **extra})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"
    row = db_row(user.sub)
    assert (row["name"], row["role"], row["email"]) == (user.name, "buyer", user.email)


def test_patch_without_profile_is_404(api, user):
    res = api.patch("/api/v1/users/me", headers=user.headers(), json={"name": "X"})
    assert res.status_code == 404


def test_users_cannot_see_each_other(api, make_cognito_user):
    alice, bob = make_cognito_user("Alice"), make_cognito_user("Bob")
    api.post("/api/v1/users/me", headers=alice.headers())
    api.post("/api/v1/users/me", headers=bob.headers())
    assert api.get("/api/v1/users/me", headers=alice.headers()).json()["name"] == "Alice"
    assert api.get("/api/v1/users/me", headers=bob.headers()).json()["name"] == "Bob"


def test_missing_identity_headers_is_401(api):
    res = api.get("/api/v1/users/me")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHENTICATED"
