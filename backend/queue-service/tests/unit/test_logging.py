from __future__ import annotations

import json
import logging
import sys

import pytest

from app.core.logging import JsonFormatter, TextFormatter, configure_logging

pytestmark = pytest.mark.unit


def _record(msg: str = "admit", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("app.queue.admitter", logging.INFO, __file__, 1, msg, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_line_carries_extra_fields() -> None:
    line = JsonFormatter().format(_record(event_id="e-1", admitted=3))
    data = json.loads(line)
    assert data["msg"] == "admit"
    assert data["level"] == "INFO"
    assert data["logger"] == "app.queue.admitter"
    assert (data["event_id"], data["admitted"]) == ("e-1", 3)
    assert data["ts"].endswith("Z") and len(data["ts"]) == len("2026-10-17T12:00:00.123Z")


def test_extras_never_overwrite_the_basics() -> None:
    data = json.loads(JsonFormatter().format(_record(level="spoofed", msg_extra="x")))
    assert data["level"] == "INFO"


def test_json_includes_exceptions_and_handles_odd_values() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = _record(when=object())
        record.exc_info = sys.exc_info()
    data = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in data["exc"]
    assert data["when"].startswith("<object")


def test_text_line_appends_extras() -> None:
    line = TextFormatter().format(_record(event_id="e-1", admitted=3))
    assert line.endswith("INFO app.queue.admitter admit event_id=e-1 admitted=3")


def test_configure_logging_installs_one_handler(capsys: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    try:
        configure_logging("debug", "json")
        assert len(root.handlers) == 1 and root.level == logging.DEBUG
        logging.getLogger("uvicorn.access").info("GET /health/live 200")
        out = capsys.readouterr().out.strip().splitlines()[-1]
        assert json.loads(out)["logger"] == "uvicorn.access"
    finally:
        root.handlers[:], root.level = saved[0], saved[1]
