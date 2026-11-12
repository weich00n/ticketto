"use client";
import { Button } from "@/components/ui/button";
import { Empty, PageHeading, Timer } from "@/components/ui/shared";
import { money } from "@/lib/format";
import {
  Elements,
  PaymentElement,
  useElements,
  useStripe,
} from "@stripe/react-stripe-js";
import { loadStripe, type Stripe } from "@stripe/stripe-js";
import { ArrowRight, ShieldCheck } from "lucide-react";
import { useState, type FormEvent } from "react";

// Stripe.js loads once per page, and only when a payment is shown.
let stripe: { key: string; promise: Promise<Stripe | null> } | undefined;
const stripeFor = (key: string) => {
  if (stripe?.key !== key) stripe = { key, promise: loadStripe(key) };
  return stripe.promise;
};

// Card details go straight to Stripe. The order is confirmed by Checkout's
// webhook, never by this page, so success only moves on to the order status.
export function Payment({
  stripeKey,
  clientSecret,
  total,
  expires,
  onExpired,
  onPaid,
}: {
  stripeKey: string;
  clientSecret: string;
  total: number;
  expires: number;
  onExpired: () => void;
  onPaid: () => void;
}) {
  if (!stripeKey)
    return (
      <Empty
        title="Payment is unavailable"
        text="Card payments are not set up on this site yet. Your reservation is kept until its timer runs out."
      />
    );
  return (
    <div className="container section">
      <PageHeading
        label="03 — PAYMENT"
        title="One last step."
        text="Pay securely with Stripe to confirm your tickets."
      />
      <div className="two-column">
        <Elements stripe={stripeFor(stripeKey)} options={{ clientSecret }}>
          <PaymentForm total={total} onPaid={onPaid} />
        </Elements>
        <aside className="panel booking-panel">
          <Timer expires={expires} onExpired={onExpired} />
          <div className="price-row total">
            <span>Total (SGD)</span>
            <b>{money(total)}</b>
          </div>
          <Button form="payment-form" type="submit">
            Pay {money(total)} <ArrowRight size={16} />
          </Button>
        </aside>
      </div>
    </div>
  );
}

function PaymentForm({ total, onPaid }: { total: number; onPaid: () => void }) {
  const stripe = useStripe();
  const elements = useElements();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const submit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!stripe || !elements || loading) return;
    setLoading(true);
    setError("");
    // Cards that need 3-D Secure leave the page and come back to the order status.
    const result = await stripe.confirmPayment({
      elements,
      redirect: "if_required",
      confirmParams: { return_url: `${location.origin}/checkout/success` },
    });
    setLoading(false);
    if (result.error)
      setError(result.error.message ?? "Your payment could not be completed.");
    else onPaid();
  };
  return (
    <form id="payment-form" onSubmit={submit} className="checkout-form">
      <section className="panel">
        <h2>Payment details</h2>
        <PaymentElement />
      </section>
      <div className="notice">
        <ShieldCheck size={20} />
        <p>
          {loading
            ? `Processing your payment of ${money(total)}…`
            : "Your card details are sent directly to Stripe and never stored by Ticketto."}
        </p>
      </div>
      {error && (
        <p className="error-message" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}
