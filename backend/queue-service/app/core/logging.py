"""Logging setup shared by the api, admitter and consumer processes.

Code logs context with `extra`, e.g.
    log.info("admit", extra={"event_id": eid, "admitted": 3})
and the formatter puts those fields on the line:
  json (production): one JSON object per line, ready for a log pipeline
      {"ts": "...", "level": "INFO", "logger": "app.queue.admitter",
       "msg": "admit", "event_id": "...", "admitted": 3}
  text (local dev):  2026-10-17T12:00:00.123Z INFO app.queue.admitter admit event_id=... admitted=3
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Literal

LogFormat = Literal["json", "text"]

# Attributes every LogRecord has; anything else on a record came from `extra`.
_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"message", "asctime", "taskName"}
)


def extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS}


def _timestamp(record: logging.LogRecord) -> str:
    ts = datetime.fromtimestamp(record.created, UTC)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": _timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "thread": record.threadName,
            "msg": record.getMessage(),
        }
        for key, value in extra_fields(record).items():
            data.setdefault(key, value)  # never let extras overwrite the basics
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = f"{_timestamp(record)} {record.levelname} {record.name} {record.getMessage()}"
        extras = " ".join(f"{k}={v}" for k, v in extra_fields(record).items())
        if extras:
            line = f"{line} {extras}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure_logging(level: str = "INFO", fmt: LogFormat = "json") -> None:
    """Replace the root handlers with one stdout handler in the chosen format.

    Uvicorn's own loggers are routed through it too, so the API emits a single
    format.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
