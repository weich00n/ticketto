"""The internal port serves only /internal/v1 and /health, so a caller let in on that port
alone (notification-service) can't reach the /api/v1 routes that trust X-User-* headers."""

import pytest
from fastapi.testclient import TestClient

from app.cognito import get_cognito
from app.config import Settings, get_settings
from app.db import get_db
from app.main import create_app
from app.serve import PUBLIC_PORT, listen_ports

from .conftest import auth_headers

INTERNAL_PORT = 8001


@pytest.fixture
def make_client(session_factory, cognito, monkeypatch):
    def _make(port: int, internal_port: int | None = INTERNAL_PORT) -> TestClient:
        if internal_port is None:
            monkeypatch.delenv("INTERNAL_PORT", raising=False)
        else:
            monkeypatch.setenv("INTERNAL_PORT", str(internal_port))
        get_settings.cache_clear()
        app = create_app()

        def override_db():
            with session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_cognito] = lambda: cognito
        return TestClient(app, base_url=f"http://testserver:{port}")

    yield _make
    get_settings.cache_clear()


def test_internal_port_serves_internal_routes(make_client, make_user):
    user = make_user()
    res = make_client(INTERNAL_PORT).get(f"/internal/v1/users/{user.user_id}")
    assert res.status_code == 200
    assert res.json()["user_id"] == user.user_id


@pytest.mark.parametrize("path", ["/health", "/health/ready"])
def test_internal_port_serves_health(make_client, path):
    assert make_client(INTERNAL_PORT).get(path).status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/users/me"),
        ("POST", "/api/v1/users/me"),
        ("PATCH", "/api/v1/users/me"),
        ("POST", "/api/v1/users/me/organiser"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
        ("GET", "/internal"),
        ("GET", "/internalx/v1/users"),
    ],
)
def test_internal_port_refuses_everything_else(make_client, make_user, cognito, method, path):
    make_user()
    cognito.fail = True  # a refused request must never reach Cognito
    res = make_client(INTERNAL_PORT).request(method, path, headers=auth_headers())
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "NOT_FOUND"


def test_internal_port_refusal_keeps_correlation_id(make_client):
    res = make_client(INTERNAL_PORT).get(
        "/api/v1/users/me", headers={**auth_headers(), "X-Correlation-Id": "abc-123"}
    )
    assert res.status_code == 404
    assert res.headers["X-Correlation-Id"] == "abc-123"


def test_public_port_still_serves_api_routes(make_client, make_user):
    make_user()
    res = make_client(PUBLIC_PORT).get("/api/v1/users/me", headers=auth_headers())
    assert res.status_code == 200


def test_without_internal_port_every_port_serves_everything(make_client, make_user):
    make_user()
    client = make_client(INTERNAL_PORT, internal_port=None)
    assert client.get("/api/v1/users/me", headers=auth_headers()).status_code == 200


def test_listen_ports():
    assert listen_ports(Settings(cognito_user_pool_id="p")) == [PUBLIC_PORT]
    assert listen_ports(Settings(cognito_user_pool_id="p", internal_port=8001)) == [
        PUBLIC_PORT,
        8001,
    ]


def test_internal_port_must_differ_from_public_port():
    with pytest.raises(ValueError):
        Settings(cognito_user_pool_id="p", internal_port=PUBLIC_PORT)
