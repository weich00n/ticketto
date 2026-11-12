import json
import logging

import pytest

from app.auth import parse_groups
from app.context import correlation_id_var
from app.db import get_db
from app.logging_config import JsonFormatter
from app.main import app
from tests.unit.conftest import auth_headers


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, set()),
        ("", set()),
        ("[]", set()),
        ("organiser", {"organiser"}),
        ("[organiser]", {"organiser"}),
        ("[organiser admin]", {"organiser", "admin"}),
        ("organiser,admin", {"organiser", "admin"}),
        ("organiser, admin", {"organiser", "admin"}),
        ('["organiser","admin"]', {"organiser", "admin"}),
        ("[organisers]", {"organisers"}),
        ("not-organiser", {"not-organiser"}),
    ],
)
def test_parse_groups(raw, expected):
    assert parse_groups(raw) == expected


def test_parse_groups_is_exact_match():
    assert "organiser" not in parse_groups("[organisers]")
    assert "organiser" not in parse_groups("not-organiser")


def test_correlation_id_echoed(client):
    res = client.get("/health", headers={"X-Correlation-Id": "abc-123"})
    assert res.headers["X-Correlation-Id"] == "abc-123"


def test_correlation_id_generated_when_missing(client):
    res = client.get("/api/v1/users/me", headers=auth_headers())
    cid = res.headers["X-Correlation-Id"]
    assert len(cid) == 36
    assert res.json()["correlation_id"] == cid


def test_unsafe_correlation_id_replaced(client):
    res = client.get("/health", headers={"X-Correlation-Id": "bad id\twith spaces"})
    assert res.headers["X-Correlation-Id"] != "bad id\twith spaces"


def test_unknown_route_uses_error_format(client):
    res = client.get("/nope", headers={"X-Correlation-Id": "cid-1"})
    assert res.status_code == 404
    assert res.json() == {
        "error": {"code": "NOT_FOUND", "message": "Not Found"},
        "correlation_id": "cid-1",
    }


def test_wrong_method_uses_error_format(client):
    res = client.delete("/api/v1/users/me", headers=auth_headers())
    assert res.status_code == 405
    assert res.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_invalid_json_is_400(client, make_user):
    make_user()
    res = client.patch(
        "/api/v1/users/me",
        headers={**auth_headers(), "Content-Type": "application/json"},
        content="{not json",
    )
    assert res.status_code == 400


def test_unexpected_error_is_500_in_standard_format(client):
    def boom():
        raise RuntimeError("database exploded")

    app.dependency_overrides[get_db] = boom
    res = client.get("/api/v1/users/me", headers={**auth_headers(), "X-Correlation-Id": "cid-500"})
    assert res.status_code == 500
    assert res.json() == {
        "error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred."},
        "correlation_id": "cid-500",
    }
    assert res.headers["X-Correlation-Id"] == "cid-500"
    assert "database exploded" not in res.text


def test_json_log_includes_correlation_id():
    token = correlation_id_var.set("cid-log")
    try:
        record = logging.makeLogRecord(
            {"name": "t", "levelname": "INFO", "msg": "hello", "user_id": "u1"}
        )
        entry = json.loads(JsonFormatter().format(record))
    finally:
        correlation_id_var.reset(token)
    assert entry["correlation_id"] == "cid-log"
    assert entry["message"] == "hello"
    assert entry["user_id"] == "u1"
    assert entry["timestamp"].endswith("Z")
