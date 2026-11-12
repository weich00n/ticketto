"""Handlers for checkout's three consumer queues.

Pure delegates: message-id deduplication and every business effect live in
CheckoutService, in the same transaction, so a redelivered message is a no-op only if
the first delivery committed. The consumer thread acks only after this returns; a
raised exception is a transient failure (requeue).
"""

from __future__ import annotations

from ..service import CheckoutService
from .envelope import Envelope


class MessageHandlers:
    def __init__(self, session_factory, service: CheckoutService) -> None:
        self._service = service

    def event_cancelled(self, envelope: Envelope) -> None:
        self._service.handle_event_cancelled(envelope)

    def reservation_expired(self, envelope: Envelope) -> None:
        self._service.handle_reservation_expired(envelope)

    def refund_required(self, envelope: Envelope) -> None:
        self._service.handle_refund_required(envelope)
