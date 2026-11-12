"""Organiser upgrade, checked end to end with real cognito-local tokens."""

import pytest

pytestmark = pytest.mark.integration


def groups_header(claims: dict) -> str:
    """Format cognito:groups the way API Gateway passes it on: e.g. "[organiser]"."""
    return "[" + " ".join(claims.get("cognito:groups", [])) + "]"


def test_upgrade_flow_with_real_tokens(api, user, sign_in, db_row):
    # Sign in for real; use the token's claims as the gateway would.
    claims = sign_in(user)
    assert claims["sub"] == user.sub and claims["username"] == user.sub
    assert "organiser" not in claims.get("cognito:groups", [])
    headers = {"X-User-Sub": claims["sub"], "X-User-Username": claims["username"]}

    assert api.post("/api/v1/users/me", headers=headers).status_code == 201

    res = api.post(
        "/api/v1/users/me/organiser", headers={**headers, "X-User-Groups": groups_header(claims)}
    )
    assert res.status_code == 200
    assert res.json()["role"] == "organiser"
    assert res.json()["token_refresh_required"] is True
    assert db_row(user.sub)["role"] == "organiser"

    # A newly issued token now carries the group...
    new_claims = sign_in(user)
    assert "organiser" in new_claims["cognito:groups"]

    # ...and with it the service reports that no refresh is needed.
    again = api.post(
        "/api/v1/users/me/organiser",
        headers={**headers, "X-User-Groups": groups_header(new_claims)},
    )
    assert again.status_code == 200
    assert again.json()["token_refresh_required"] is False


def test_upgrade_without_profile_is_404_and_leaves_cognito_alone(api, user, sign_in):
    res = api.post("/api/v1/users/me/organiser", headers=user.headers())
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "USER_NOT_FOUND"
    assert "organiser" not in sign_in(user).get("cognito:groups", [])


def test_organiser_keeps_profile_data(api, user):
    created = api.post("/api/v1/users/me", headers=user.headers()).json()
    upgraded = api.post("/api/v1/users/me/organiser", headers=user.headers()).json()
    for field in ("user_id", "email", "name", "created_at"):
        assert upgraded[field] == created[field]
