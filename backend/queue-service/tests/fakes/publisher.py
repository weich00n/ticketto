"""Publishing as the other services do: persistent messages, publisher confirms,
AMQP message_id/type mirroring the envelope. Each fake records what it sent."""

from __future__ import annotations

import pika
from pika.adapters.blocking_connection import BlockingChannel

from tests.fakes.messages import OutgoingMessage


class FakeService:
    """Base for FakeEventService, FakeReservationService and FakeCheckoutService."""

    exchange: str = ""

    def __init__(self, channel: BlockingChannel):
        self._channel = channel
        if not getattr(channel, "_delivery_confirmation", False):  # several fakes, one channel
            self._channel.confirm_delivery()
        self.sent: list[OutgoingMessage] = []

    def publish(self, message: OutgoingMessage) -> None:
        assert message.exchange == self.exchange, f"{type(self).__name__} doesn't own that"
        self.publish_raw(
            message.routing_key,
            message.body,
            message_id=message.message_id,
            message_type=message.message_type,
        )
        self.sent.append(message)

    def publish_raw(
        self,
        routing_key: str,
        body: bytes,
        *,
        message_id: str | None = None,
        message_type: str | None = None,
    ) -> None:
        """Arbitrary bytes, e.g. poison messages."""
        self._channel.basic_publish(
            self.exchange,
            routing_key,
            body,
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,  # persistent
                message_id=message_id,
                type=message_type,
            ),
            mandatory=True,
        )
