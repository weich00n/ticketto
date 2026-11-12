import json
import logging
from datetime import UTC, datetime

from .correlation import get_correlation_id

_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Fields passed in `extra=` are logged too."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": get_correlation_id(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    gunicorn = logging.getLogger("gunicorn.error")
    gunicorn.handlers = []
    gunicorn.propagate = True
    # pika logs every connection at INFO, and the worker connects every cycle.
    logging.getLogger("pika").setLevel(logging.WARNING)
    logging.getLogger("stripe").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
