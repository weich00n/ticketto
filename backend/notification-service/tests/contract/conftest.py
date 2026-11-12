"""Adapters between rabbitmq-service's generated contract tests and notification-service's real
code.

contract_queues: the queues each run drains (app.broker.QUEUES).

contract_handle: the real decision for one delivery, app.broker.dispose, running the real
handler (handlers.handle_message) on the unit tests' SQLite database, with their fakes for
user-service and event-service: ack, requeue, or dead-letter (reported as "reject").
OrderRefunded and RefundFailed are retried while user-service doesn't know the buyer
(UserNotFound), so the buyer the body names is known to the fake first, as in production,
where the buyer's profile exists before they can order.
"""

from __future__ import annotations

import json

import pytest

from app.broker import QUEUES, Disposition, dispose
from app.handlers import handle_message
from app.user_client import Recipient
from tests.unit import conftest as unit

# The unit tests' fixtures, re-exported so pytest finds them here: settings and a SQLite
# database, and the user-service and event-service fakes.
settings = unit.settings
session_factory = unit.session_factory
users = unit.users
events = unit.events

OUTCOMES = {
    Disposition.ACK: "ack",
    Disposition.REQUEUE: "requeue",
    Disposition.DEAD_LETTER: "reject",
}


def _known_buyer(users, body: bytes) -> None:
    """Make the buyer a body names (payload.user_id) known to the user-service fake."""
    try:
        buyer = str(json.loads(body)["payload"]["user_id"]).strip().lower()
    except (ValueError, TypeError, KeyError):
        return  # names no buyer: the handler decides what to do with it
    users.users_by_sub.setdefault(
        buyer,
        Recipient(
            user_id="00000000-0000-4000-8000-000000000001",
            email="buyer@example.com",
            name="Contract Buyer",
            cognito_sub=buyer,
        ),
    )


@pytest.fixture
def contract_queues() -> set[str]:
    return set(QUEUES)


@pytest.fixture
def contract_handle(session_factory, users, events):
    def handle(_queue: str, body: bytes, properties: dict[str, object]) -> str:
        _known_buyer(users, body)
        disposition = dispose(
            body,
            lambda message: handle_message(session_factory, users, events, message),
            correlation_id=properties.get("correlation_id"),
        )
        return OUTCOMES[disposition]

    return handle
