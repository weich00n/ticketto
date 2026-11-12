"""Adapter between rabbitmq-service's generated contract tests and event-service's real code.

contract_publish makes event-service publish one message the way production does:
- the HTTP API publishes (and, for EventCancelled, cancels) an event, which writes the outbox
  rows in the same transaction as the change (app/lifecycle.py, app/outbox.py)
- the real outbox relay, publish_pending_messages, sends every pending row. Only its broker
  connection is replaced: its channel is ticketto_contracts' RecordingChannel, which records
  the exchange declare, publisher confirms and every basic_publish for the contract checks.

The app, database and request fixtures are the unit tests' (tests/unit/conftest.py).
"""

import pytest

from app import outbox
from tests.unit import conftest as unit

# The unit tests' fixtures, re-exported so pytest finds them here: a Flask app on a throwaway
# SQLite database, its test client, and an organiser and an event to publish.
app = unit.app
client = unit.client
session_factory = unit.session_factory
organiser_headers = unit.organiser_headers
release_at = unit.release_at
event_payload = unit.event_payload

MY_EVENTS = "/api/v1/organizer/events"

# What an organiser does for each message event-service publishes.
ACTIONS = {
    "EventPublished": ["publish"],
    "EventCancelled": ["publish", "cancel"],
}


class _Connection:
    """Stands in for pika.BlockingConnection, handing the relay the recording channel."""

    def __init__(self, channel):
        self._channel = channel

    def channel(self):
        return self._channel

    def close(self):
        pass


@pytest.fixture()
def contract_publish(monkeypatch, client, session_factory, organiser_headers, event_payload):
    def publish(message_name, channel):
        created = client.post(MY_EVENTS, json=event_payload, headers=organiser_headers)
        assert created.status_code == 201, created.get_json()
        url = f"{MY_EVENTS}/{created.get_json()['event_id']}"
        for action in ACTIONS[message_name]:
            body = {"reason": "Venue closure"} if action == "cancel" else None
            response = client.post(f"{url}/{action}", json=body, headers=organiser_headers)
            assert response.status_code == 200, response.get_json()
        monkeypatch.setattr(outbox.pika, "BlockingConnection", lambda _params: _Connection(channel))
        outbox.publish_pending_messages(session_factory, "amqp://unused")

    return publish
