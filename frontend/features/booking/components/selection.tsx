"use client";

import { Button } from "@/components/ui/button";
import { Empty, PageHeading, Timer } from "@/components/ui/shared";
import { dateLabel, money } from "@/lib/format";
import { type Event, type Reservation } from "@/types/ticketing";
import { ArrowLeft, ArrowRight, Clock3, Minus, Plus } from "lucide-react";
import Link from "next/link";
import { useCallback, useState } from "react";

export function Selection({
  event,
  reserve,
  admitted,
  selectionExpires,
  bookingFee = 4,
}: {
  event: Event;
  // A rejected promise shows its message under the button.
  reserve: (r: Reservation) => void | Promise<void>;
  admitted: boolean;
  // When the buyer's time to choose ends, as a local clock time in milliseconds.
  selectionExpires?: number;
  bookingFee?: number;
}) {
  const bookingLimit = Math.min(5, event.maxTickets);
  const [cat, setCat] = useState(
    event.categories.find((c) => c.available > 0)?.id || "",
  );
  const [qty, setQty] = useState(1);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [timedOut, setTimedOut] = useState(false);
  const onSelectionExpired = useCallback(() => setTimedOut(true), []);
  const chosen = event.categories.find((c) => c.id === cat);
  if (
    ["Sold out", "Event cancelled", "Event ended", "Draft"].includes(
      event.status,
    )
  )
    return (
      <Empty
        title="Tickets are unavailable"
        text={`This event is ${event.status.toLowerCase()}.`}
      />
    );
  if (timedOut)
    return (
      <Empty
        title="Your selection time has ended"
        text="You have 5 minutes to choose tickets after leaving the waiting room, and no tickets were held. Join the waiting room to try again."
        action={
          <Button asChild>
            <Link href={`/queue/${event.id}`}>Join Waiting Room</Link>
          </Button>
        }
      />
    );
  if (event.waitingRoom && !admitted)
    return (
      <Empty
        title="Your place begins in the waiting room"
        text="Join the waiting room first. When it is your turn, you can choose your tickets."
        action={
          <Button asChild>
            <Link href={`/queue/${event.id}`}>Join Waiting Room</Link>
          </Button>
        }
      />
    );
  return (
    <div className="container section">
      <Link className="back" href={`/events/${event.id}`}>
        <ArrowLeft size={15} /> Back to event
      </Link>
      <PageHeading
        label="01 — MAKE IT YOURS"
        title="A place in the experience."
        text={`${event.name} · ${dateLabel(event.date)}`}
      />
      {selectionExpires !== undefined && (
        <p className="small muted">
          Choose your tickets before the time runs out:{" "}
          <Timer expires={selectionExpires} onExpired={onSelectionExpired} />
        </p>
      )}
      <div className="two-column">
        <div className="ticket-options">
          {event.categories.map((c) => (
            <button
              key={c.id}
              disabled={!c.available}
              aria-pressed={cat === c.id}
              className={`category-card ${cat === c.id ? "chosen" : ""}`}
              onClick={() => {
                setCat(c.id);
                setQty(Math.min(qty, c.available));
              }}
            >
              <span className="radio-dot" />
              <div>
                <h3>{c.name}</h3>
                <p>{c.benefits}</p>
                <span className={c.available < 30 ? "low-stock" : "muted"}>
                  {c.available
                    ? `${c.available} tickets remaining`
                    : "Sold out"}
                </span>
              </div>
              <b>{money(c.price)}</b>
            </button>
          ))}
          <div className="panel">
            <div className="quantity-row">
              <div>
                <h3>Make room for good company.</h3>
                <p>Maximum {bookingLimit} tickets per booking</p>
              </div>
              <div className="stepper">
                <button
                  aria-label="Decrease quantity"
                  disabled={qty === 1}
                  onClick={() => setQty(qty - 1)}
                >
                  <Minus size={16} />
                </button>
                <b>{qty}</b>
                <button
                  aria-label="Increase quantity"
                  disabled={
                    qty >= Math.min(bookingLimit, chosen?.available || 0)
                  }
                  onClick={() => setQty(qty + 1)}
                >
                  <Plus size={16} />
                </button>
              </div>
            </div>
          </div>
        </div>
        <aside className="panel booking-panel">
          <span className="eyebrow">YOUR EVENING, AT A GLANCE</span>
          <h2>Order summary</h2>
          <h3>{event.name}</h3>
          <p>
            {dateLabel(event.date)} · {event.time}
          </p>
          <div className="price-row">
            <span>
              {chosen?.name} × {qty}
            </span>
            <b>{money((chosen?.price || 0) * qty)}</b>
          </div>
          {bookingFee > 0 && (
            <div className="price-row">
              <span>Booking fee</span>
              <b>{money(qty * bookingFee)}</b>
            </div>
          )}
          <div className="price-row total">
            <span>Total</span>
            <b>{money(((chosen?.price || 0) + bookingFee) * qty)}</b>
          </div>
          <Button
            disabled={!chosen || pending}
            onClick={async () => {
              if (!chosen) return;
              setPending(true);
              setError("");
              try {
                await reserve({
                  eventId: event.id,
                  categoryId: chosen.id,
                  category: chosen.name,
                  quantity: qty,
                  price: chosen.price,
                  seats: Array.from(
                    { length: qty },
                    (_, i) =>
                      `${cat === "cat1" ? "A" : cat === "cat2" ? "F" : "K"}${12 + i + Math.max(0, (cat === "cat1" ? 28 : cat === "cat2" ? 124 : 240) - chosen.available)}`,
                  ),
                  expires: Date.now() + 300000,
                });
              } catch (e) {
                setError(
                  e instanceof Error
                    ? e.message
                    : "We could not reserve these tickets.",
                );
              } finally {
                setPending(false);
              }
            }}
          >
            {pending ? "Reserving…" : "Reserve Tickets"}{" "}
            <ArrowRight size={16} />
          </Button>
          {error && (
            <p className="error-message" role="alert">
              {error}
            </p>
          )}
          <p className="small muted">
            <Clock3 size={14} /> Your tickets will be held for five minutes.
          </p>
        </aside>
      </div>
    </div>
  );
}
