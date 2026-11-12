from __future__ import annotations

import subprocess
import sys

import pytest

from workers.healthcheck import HEARTBEAT_KEYS, evaluate

pytestmark = pytest.mark.unit

NOW = 1_795_176_000_000


def test_fresh_heartbeats_are_healthy() -> None:
    assert evaluate({"a": str(NOW - 500), "b": str(NOW)}, NOW, 10_000) == []


def test_missing_stale_and_garbage_heartbeats_are_reported() -> None:
    problems = evaluate(
        {"missing": None, "stale": str(NOW - 12_000), "junk": "abc", "ok": str(NOW)},
        NOW,
        10_000,
    )
    assert problems == [
        "missing: no heartbeat",
        "stale: last heartbeat 12.0s ago",
        "junk: unreadable heartbeat 'abc'",
    ]


def test_consumer_check_covers_all_three_queues() -> None:
    assert HEARTBEAT_KEYS["consumer"] == [
        "queue:consumer:heartbeat:queue-service.EventPublished",
        "queue:consumer:heartbeat:queue-service.EventCancelled",
        "queue:consumer:heartbeat:queue-service.EventSoldOut",
        "queue:consumer:heartbeat:queue-service.OrderConfirmed",
    ]
    assert HEARTBEAT_KEYS["admitter"] == ["queue:admitter:heartbeat"]


def test_healthcheck_imports_stay_light() -> None:
    """ECS runs the check as a new process on 0.25 vCPU with a time limit; loading
    SQLAlchemy, pika or the app's handlers made it time out (staging, 2026-10-28)."""
    heavy = ("sqlalchemy", "pika", "fastapi", "asyncmy", "jwt", "app.messaging.consumers")
    code = f"import sys, workers.healthcheck; print([m for m in {heavy!r} if m in sys.modules])"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
