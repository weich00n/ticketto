"""The parameters checkout sends to Stripe: the API rejects combinations the fake
client in tests/fakes would happily accept, so check them at the SDK boundary."""

from types import SimpleNamespace

import pytest
import stripe

from app.clients.stripe import HttpStripeClient
from app.service import _stripe_reason

# What Stripe's Refund.create accepts for `reason`.
STRIPE_REFUND_REASONS = {"duplicate", "fraudulent", "requested_by_customer"}


def test_payment_intent_does_not_mix_payment_method_parameters(monkeypatch):
    sent = {}

    def fake_create(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(id="pi_1", client_secret="pi_1_secret")

    monkeypatch.setattr(stripe.PaymentIntent, "create", fake_create)
    created = HttpStripeClient("sk_test_x", "whsec_x").create_payment_intent(
        order_id="o-1", hold_id="h-1", amount=10000, currency="sgd"
    )

    # Stripe answers 400 when both are present.
    assert not ("payment_method_types" in sent and "automatic_payment_methods" in sent)
    assert sent["payment_method_types"] == ["card"]
    assert sent["idempotency_key"] == "o-1"
    assert created.client_secret == "pi_1_secret"


@pytest.mark.parametrize(
    "reason",
    ["event_cancelled", "expired_payment", "refund_required", "confirm_rejected", "late_payment"],
)
def test_refund_reasons_are_ones_stripe_accepts(reason):
    assert _stripe_reason(reason) in STRIPE_REFUND_REASONS


def test_unknown_refund_reason_falls_back_to_an_accepted_one():
    assert _stripe_reason("something_new") in STRIPE_REFUND_REASONS
