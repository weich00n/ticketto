import uuid

import pytest

pytestmark = pytest.mark.integration


def test_internal_lookup(api, user):
    created = api.post("/api/v1/users/me", headers=user.headers()).json()
    res = api.get(f"/internal/v1/users/{created['user_id']}")
    assert res.status_code == 200
    assert res.json()["email"] == user.email


def test_internal_lookup_unknown_and_invalid(api):
    assert api.get(f"/internal/v1/users/{uuid.uuid4()}").status_code == 404
    assert api.get("/internal/v1/users/not-a-uuid").status_code == 400


def test_correlation_id_round_trip(api, user):
    res = api.get("/api/v1/users/me", headers={**user.headers(), "X-Correlation-Id": "it-corr-123"})
    assert res.headers["X-Correlation-Id"] == "it-corr-123"
    assert res.json()["correlation_id"] == "it-corr-123"


def test_unknown_route_uses_error_format(api):
    res = api.get("/api/v1/nope")
    assert res.status_code == 404
    assert set(res.json()) == {"error", "correlation_id"}
