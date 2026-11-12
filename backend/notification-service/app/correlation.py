"""Correlation ID for log lines: the AMQP correlation_id of the message being handled, so
one request can be followed from the publisher's logs to the email that went out."""

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")


def get_correlation_id() -> str:
    return correlation_id_var.get()


@contextmanager
def bound_correlation_id(value: str | None) -> Iterator[str]:
    """Use `value` (or a new UUID if it is missing or unsafe to log) inside the block."""
    cleaned = value if value and _VALID_ID.match(value) else str(uuid.uuid4())
    token = correlation_id_var.set(cleaned)
    try:
        yield cleaned
    finally:
        correlation_id_var.reset(token)
