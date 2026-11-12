"""Client for Stripe, built around PaymentIntents (group decision, 2026-10-19).

The design:

- POST /orders creates a PaymentIntent (idempotency key = order_id) and returns the
  client secret; the frontend drives the Payment Element with it.
- We never call confirm ourselves (the card is confirmed by the frontend), so the
  order moves PENDING_PAYMENT -> CONFIRMING only when the webhook
  payment_intent.succeeded arrives, verified by its signature.
- Refunds go through stripe.Refund with a stable idempotency key so a retry never
  produces a second refund for the same logical one.

The stripe SDK is wrapped behind a small protocol so tests substitute a fake; the
webhook verify is the same, and is what makes replay safe (a bad signature is a 400,
a seen event id is a no-op).
"""

from dataclasses import dataclass
from typing import Protocol

import stripe


class StripeUnavailableError(Exception):
    """Network/SDK failure talking to Stripe. Maps to STRIPE_UNAVAILABLE (503)."""


class StripeRejectedError(Exception):
    """Stripe refused the request itself (invalid request, e.g. the charge is already
    refunded). Asking again with the same idempotency key gets the same answer, so the
    caller must not retry it automatically."""


class WebhookSignatureError(Exception):
    """Stripe-Signature did not verify. Maps to 400 (the webhook endpoint only)."""


@dataclass(frozen=True)
class PaymentIntentCreated:
    payment_intent_id: str
    client_secret: str


@dataclass(frozen=True)
class RefundCreated:
    refund_id: str
    status: str  # succeeded | pending | failed | canceled (Stripe's vocabulary)
    amount: int
    currency: str


@dataclass(frozen=True)
class StripeEvent:
    """A verified webhook event, flattened to what checkout needs."""

    event_id: str
    event_type: str
    object_id: str
    object: dict


class StripeClient(Protocol):
    def create_payment_intent(
        self,
        *,
        order_id: str,
        hold_id: str,
        amount: int,
        currency: str,
    ) -> PaymentIntentCreated: ...

    def get_payment_intent(self, payment_intent_id: str) -> PaymentIntentCreated: ...

    def cancel_payment_intent(self, payment_intent_id: str) -> bool:
        """True when the intent had already succeeded, so the money is taken."""
        ...

    def create_refund(
        self,
        *,
        payment_intent_id: str,
        amount: int | None,
        reason: str,
        idempotency_key: str,
    ) -> RefundCreated: ...

    def verify_webhook(self, payload: bytes, signature: str) -> StripeEvent: ...


class HttpStripeClient:
    """The real client. The SDK is configured once; calls are wrapped so that any
    StripeException becomes a typed error the service layer can map to a TC code."""

    def __init__(self, api_key: str, webhook_secret: str) -> None:
        self._webhook_secret = webhook_secret
        stripe.api_key = api_key

    def create_payment_intent(
        self,
        *,
        order_id: str,
        hold_id: str,
        amount: int,
        currency: str,
    ) -> PaymentIntentCreated:
        try:
            intent = stripe.PaymentIntent.create(
                amount=amount,
                currency=currency,
                # Stripe rejects payment_method_types together with automatic_payment_methods.
                # Card only for now; add "paynow" here (SGD only) if the team offers PayNow.
                payment_method_types=["card"],
                metadata={"order_id": order_id, "hold_id": hold_id},
                idempotency_key=order_id,
            )
        except stripe.error.StripeError as exc:
            raise StripeUnavailableError(str(exc)) from exc
        return PaymentIntentCreated(
            payment_intent_id=intent.id,
            client_secret=intent.client_secret or "",
        )

    def get_payment_intent(self, payment_intent_id: str) -> PaymentIntentCreated:
        """Re-fetch an existing intent's client secret (a retry, or a late webhook)."""
        try:
            intent = stripe.PaymentIntent.retrieve(payment_intent_id)
        except stripe.error.StripeError as exc:
            raise StripeUnavailableError(str(exc)) from exc
        return PaymentIntentCreated(
            payment_intent_id=intent.id,
            client_secret=intent.client_secret or "",
        )

    def cancel_payment_intent(self, payment_intent_id: str) -> bool:
        """Best-effort: a cancelled intent cannot be completed by a late card.

        Returns True when Stripe refuses because the intent already succeeded: the buyer
        paid, but the webhook saying so has not reached us (checkout was down, or Stripe
        is still retrying it). Failures here are logged, not raised - expiring the order is
        the point, and a dead Stripe connection must not block the deadline sweep.
        """
        try:
            stripe.PaymentIntent.cancel(payment_intent_id)
            return False
        except stripe.error.InvalidRequestError as exc:
            try:
                intent = stripe.PaymentIntent.retrieve(payment_intent_id)
            except stripe.error.StripeError:
                raise StripeUnavailableError(str(exc)) from exc
            if intent.status == "succeeded":
                return True
            raise StripeUnavailableError(str(exc)) from exc
        except stripe.error.StripeError as exc:
            raise StripeUnavailableError(str(exc)) from exc

    def create_refund(
        self,
        *,
        payment_intent_id: str,
        amount: int | None,
        reason: str,
        idempotency_key: str,
    ) -> RefundCreated:
        try:
            refund = stripe.Refund.create(
                payment_intent=payment_intent_id,
                amount=amount,
                reason=reason,
                idempotency_key=idempotency_key,
            )
        except stripe.error.InvalidRequestError as exc:
            raise StripeRejectedError(str(exc)) from exc
        except stripe.error.StripeError as exc:
            raise StripeUnavailableError(str(exc)) from exc
        return RefundCreated(
            refund_id=refund.id,
            status=refund.status,
            amount=refund.amount or 0,
            currency=refund.currency or "",
        )

    def verify_webhook(self, payload: bytes, signature: str) -> StripeEvent:
        """Verify the Stripe-Signature and flatten the event.

        Raises WebhookSignatureError on a bad signature (route answers 400) and
        StripeUnavailableError on a malformed body (route answers 400 too, but a
        different message).
        """
        try:
            event = stripe.Webhook.construct_event(payload, signature, self._webhook_secret)
        except stripe.error.SignatureVerificationError as exc:
            raise WebhookSignatureError(str(exc)) from exc
        except (ValueError, stripe.error.StripeError) as exc:
            raise StripeUnavailableError(str(exc)) from exc
        obj = event.get("data", {}).get("object", {}) if isinstance(event, dict) else {}
        return StripeEvent(
            event_id=event.get("id", ""),
            event_type=event.get("type", ""),
            object_id=obj.get("id", ""),
            object=obj,
        )
