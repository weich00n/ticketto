"""Stand-in for the Checkout service: publishes OrderConfirmed."""

from __future__ import annotations

from tests.fakes.publisher import FakeService


class FakeCheckoutService(FakeService):
    exchange = "checkout.events"
