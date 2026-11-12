"""Adapters between rabbitmq-service's generated contract tests and Queue's real code.

contract_queues: the queue names the consumer process listens on (CONSUMER_QUEUES).

contract_handle: the consumer's real accept/reject decision for one delivery, `dispose()`,
which runs `parse_message` (envelope and payload validation) and maps the result exactly as
the consumer does: ack, requeue, or dead-letter (nack without requeue, reported as "reject").
The handler that `dispose` calls is a no-op here: it would write MySQL and Redis, which the
integration tests in tests/integration/messaging cover. Every payload field a handler can read
is declared, and validated, in app/messaging/schemas.py, so parsing decides whether Queue can
use a message.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.messaging.consumers import Disposition, dispose
from app.messaging.schemas import Message
from app.messaging.topology import CONSUMER_QUEUES

OUTCOMES = {
    Disposition.ACK: "ack",
    Disposition.REQUEUE: "requeue",
    Disposition.DEAD_LETTER: "reject",
}

Handle = Callable[[str, bytes, dict[str, object]], str]


def _no_side_effects(_message: Message) -> None:
    """Stands in for MessageHandlers.handle: parsing has already decided the outcome."""


@pytest.fixture
def contract_queues() -> set[str]:
    return {queue.name for queue in CONSUMER_QUEUES}


@pytest.fixture
def contract_handle() -> Handle:
    def handle(_queue: str, body: bytes, _properties: dict[str, object]) -> str:
        return OUTCOMES[dispose(body, _no_side_effects)]

    return handle
