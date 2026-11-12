"use client";
/* eslint-disable @next/next/no-img-element */
import { Button } from "@/components/ui/button";
import { Empty, Field, PageHeading, Timer } from "@/components/ui/shared";
import { dateLabel, money } from "@/lib/format";
import {
  type DigitalTicket,
  type Event,
  type Order,
  type Reservation,
} from "@/types/ticketing";
import {
  ArrowRight,
  Check,
  CreditCard,
  MapPin,
  ShieldCheck,
} from "lucide-react";
import Link from "next/link";
import { useState, type FormEvent } from "react";

export type PaymentDetails = { name: string; email: string; decline: boolean };

const demoBookingFee = 4;

// Builds the order and tickets a successful demo payment produces.
export function demoOrder(
  r: Reservation,
  event: Event,
  details: PaymentDetails,
): { order: Order; tickets: DigitalTicket[] } {
  const id = `AE-${Date.now().toString(36).toUpperCase()}`;
  const order: Order = {
    id,
    reservation: r,
    buyer: details.name,
    email: details.email,
    total: (r.price + demoBookingFee) * r.quantity,
    date: new Date().toISOString(),
  };
  const tickets = r.seats.map((seat, i) => ({
    id: `${id}-${i + 1}`,
    eventId: event.id,
    buyer: order.buyer,
    category: r.category,
    seat,
    status: "Valid" as const,
  }));
  return { order, tickets };
}

export function Checkout({
  reservation: r,
  events,
  pay,
  simulated = true,
}: {
  reservation: Reservation | null;
  events: Event[];
  // Resolves once payment is under way; a rejected promise shows its message.
  pay: (details: PaymentDetails) => Promise<void>;
  // Simulated checkouts offer demo payment options and charge a booking fee.
  simulated?: boolean;
}) {
  const [loading, setLoading] = useState(false);
  const [fail, setFail] = useState(false);
  const [error, setError] = useState("");
  const [expired, setExpired] = useState(() => !!r && Date.now() >= r.expires);
  const event = events.find((e) => e.id === r?.eventId);
  if (!r || !event)
    return (
      <Empty
        title="Your next great evening is waiting."
        text="Select tickets to begin your booking."
      />
    );
  if (expired)
    return (
      <Empty
        title="Your reservation has expired"
        text={`Your five-minute hold has ended. ${event.waitingRoom ? "Join the waiting room to try again." : "Please select tickets again."}`}
        action={
          <Button asChild>
            <Link
              href={
                event.waitingRoom
                  ? `/queue/${event.id}`
                  : `/events/${event.id}/tickets`
              }
            >
              {event.waitingRoom ? "Join Waiting Room" : "Select Tickets"}
            </Link>
          </Button>
        }
      />
    );
  if (event.status === "Event cancelled")
    return (
      <Empty
        title="This event has been cancelled"
        text="No payment has been taken."
      />
    );
  const fee = simulated ? demoBookingFee : 0;
  const total = (r.price + fee) * r.quantity;
  const submit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const data = new FormData(e.currentTarget);
    if (Date.now() >= r.expires) {
      setExpired(true);
      return;
    }
    setLoading(true);
    setError("");
    try {
      await pay({
        name: String(data.get("name")),
        email: String(data.get("email")),
        decline: fail,
      });
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "The payment could not be started.",
      );
    } finally {
      setLoading(false);
    }
  };
  return (
    <div className="container section">
      <PageHeading
        label="02 — ALMOST THERE"
        title="Let’s make it a date."
        text="A few details, and the evening is yours."
      />
      <div className="two-column">
        <form id="checkout-form" onSubmit={submit} className="checkout-form">
          <section className="panel">
            <h2>Your details</h2>
            <div className="form-grid">
              <Field label="Full name">
                <input
                  required
                  name="name"
                  autoComplete="name"
                  placeholder="e.g. Alex Tan"
                  maxLength={80}
                />
              </Field>
              <Field label="Email address">
                <input
                  required
                  type="email"
                  name="email"
                  autoComplete="email"
                  placeholder="alex@example.com"
                />
              </Field>
            </div>
            <p className="small muted">
              Your digital tickets will be available in My Tickets after
              checkout.
            </p>
          </section>
          {simulated && (
            <section className="panel">
              <h2>Payment method</h2>
              <div className="payment-options">
                <button type="button" className="selected">
                  <CreditCard size={22} />
                  <b>Card</b>
                  <Check size={17} />
                </button>
              </div>
              <div className="notice">
                <ShieldCheck size={20} />
                <div>
                  <b>This is a simulated payment.</b>
                  <p>
                    A demo card is ready to use. No card number is needed. No
                    real money will be charged.
                  </p>
                </div>
              </div>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={fail}
                  onChange={(e) => setFail(e.target.checked)}
                />
                Demo: simulate a declined payment
              </label>
            </section>
          )}
          <label className="checkbox-label">
            <input type="checkbox" required />
            {simulated
              ? "I acknowledge that this is a demo booking with no monetary value and agree to the event entry instructions."
              : "I agree to the event entry instructions."}
          </label>
          {error && (
            <p className="error-message" role="alert">
              {error}
            </p>
          )}
        </form>
        <aside className="panel booking-panel">
          <Timer expires={r.expires} onExpired={() => setExpired(true)} />
          <h2>Your reservation</h2>
          <img className="summary-image" src={event.image} alt={event.name} />
          <h3>{event.name}</h3>
          <p>
            {dateLabel(event.date)} · {event.time}
          </p>
          <p>
            <MapPin size={14} />
            {event.venue}
          </p>
          <div className="notice">
            {r.category}
            {r.seats.length > 0 && ` · Seats ${r.seats.join(", ")}`}
          </div>
          <div className="price-row">
            <span>
              {r.quantity} × {money(r.price)}
            </span>
            <b>{money(r.price * r.quantity)}</b>
          </div>
          {fee > 0 && (
            <div className="price-row">
              <span>Booking fee</span>
              <b>{money(fee * r.quantity)}</b>
            </div>
          )}
          <div className="price-row total">
            <span>Total (SGD)</span>
            <b>{money(total)}</b>
          </div>
          <Button form="checkout-form" type="submit" disabled={loading}>
            {loading ? (
              <>
                <span className="spinner" />
                {simulated ? "Processing payment…" : "Preparing payment…"}
              </>
            ) : (
              <>
                {simulated ? `Pay ${money(total)}` : "Continue to payment"}{" "}
                <ArrowRight size={16} />
              </>
            )}
          </Button>
          <p className="small muted">
            {simulated
              ? "Demo payment · No real payment details collected"
              : "You will enter your card details on the next step."}
          </p>
        </aside>
      </div>
    </div>
  );
}
