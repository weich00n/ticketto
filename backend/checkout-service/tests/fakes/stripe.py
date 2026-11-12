"""In-memory stand-in for the Stripe client.

Captures every intent/refund it is asked to create so tests can assert on them, and
offers helpers to build the webhook events the service processes.
"""

from __future__ import annotations

import json
import uuid

from app.clients.stripe import (
    PaymentIntentCreated,
    RefundCreated,
    StripeEvent,
    StripeUnavailableError,
    WebhookSignatureError,
)


class FakeStripe:  # pylint: disable=too-many-instance-attributes  # one knob per scenario
    def __init__(self) -> None:
        self.intents: dict[str, PaymentIntentCreated] = {}
        self.refunds: list[RefundCreated] = []
        self.cancelled: list[str] = []
        self.already_succeeded: set[str] = set()  # intents a cancel finds already paid
        self.refund_outcome = "succeeded"  # succeeded | pending | failed
        self.refund_error: Exception | None = None  # raised by create_refund when set
        self.intent_failures = 0  # how many create_payment_intent calls fail first
        self.refund_calls: list[str] = []  # idempotency keys, one per create_refund call

    def create_payment_intent(  # pylint: disable=unused-argument
        self, *, order_id, hold_id, amount, currency
    ) -> PaymentIntentCreated:
        if self.intent_failures > 0:
            self.intent_failures -= 1
            raise StripeUnavailableError("stripe is down")
        pi = f"pi_{uuid.uuid4().hex[:12]}"
        created = PaymentIntentCreated(payment_intent_id=pi, client_secret=f"{pi}_secret")
        self.intents[pi] = created
        return created

    def get_payment_intent(self, payment_intent_id: str) -> PaymentIntentCreated:
        if payment_intent_id not in self.intents:
            raise StripeUnavailableError("no such intent")
        return self.intents[payment_intent_id]

    def cancel_payment_intent(self, payment_intent_id: str) -> bool:
        self.cancelled.append(payment_intent_id)
        return payment_intent_id in self.already_succeeded

    def create_refund(  # pylint: disable=unused-argument
        self, *, payment_intent_id, amount, reason, idempotency_key
    ) -> RefundCreated:
        self.refund_calls.append(idempotency_key)
        if self.refund_error is not None:
            raise self.refund_error
        result = RefundCreated(
            refund_id=f"re_{uuid.uuid4().hex[:12]}",
            status=self.refund_outcome,
            amount=amount or 0,
            currency="SGD",
        )
        self.refunds.append(result)
        return result

    def verify_webhook(self, payload: bytes, signature: str) -> StripeEvent:
        """Decode a Stripe-shaped event body straight into a StripeEvent (tests send
        `{"id":..., "type":..., "data":{"object":{...}}}`). A signature of "bad" fails,
        mimicking a bad Stripe-Signature."""
        if signature == "bad":
            raise WebhookSignatureError("bad signature")
        body = json.loads(payload)
        obj = body.get("data", {}).get("object", {})
        return StripeEvent(
            event_id=body.get("id", ""),
            event_type=body.get("type", ""),
            object_id=obj.get("id", ""),
            object=obj,
        )


def payment_succeeded_event(payment_intent_id: str, event_id: str = "evt_1") -> StripeEvent:
    return StripeEvent(
        event_id=event_id,
        event_type="payment_intent.succeeded",
        object_id=payment_intent_id,
        object={"id": payment_intent_id},
    )


def payment_failed_event(payment_intent_id: str, event_id: str = "evt_f") -> StripeEvent:
    return StripeEvent(
        event_id=event_id,
        event_type="payment_intent.payment_failed",
        object_id=payment_intent_id,
        object={"id": payment_intent_id},
    )


def charge_refunded_event(
    payment_intent_id: str, refund_id: str | None = None, event_id: str = "evt_r"
) -> StripeEvent:
    """charge.refunded carries the Charge (ch_...), which names its payment intent; the
    refunds list is only there on API versions that include it."""
    charge = {
        "id": f"ch_{uuid.uuid4().hex[:12]}",
        "object": "charge",
        "payment_intent": payment_intent_id,
        "refunded": True,
    }
    if refund_id is not None:
        charge["refunds"] = {"data": [{"id": refund_id, "status": "succeeded"}]}
    return StripeEvent(
        event_id=event_id,
        event_type="charge.refunded",
        object_id=charge["id"],
        object=charge,
    )
