"""Stand-in for the Event service: publishes event lifecycle messages."""

from __future__ import annotations

from tests.fakes.publisher import FakeService


class FakeEventService(FakeService):
    exchange = "event.events"
