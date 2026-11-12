"use client";
/* eslint-disable @next/next/no-img-element */
import { Button } from "@/components/ui/button";
import { Badge, ReleaseCountdown } from "@/components/ui/shared";
import { dateLabel, money, singaporeDateTime } from "@/lib/format";
import { type Event } from "@/types/ticketing";
import {
  ArrowLeft,
  ArrowRight,
  CalendarDays,
  Clock3,
  MapPin,
  ShieldCheck,
  Ticket,
} from "lucide-react";
import Link from "next/link";

export function EventDetail({
  event: e,
  demo = false,
}: {
  event: Event;
  // The demo store simulates payments; the live site takes Stripe test payments.
  demo?: boolean;
}) {
  const disabled = [
    "Sold out",
    "Event cancelled",
    "Event ended",
    "Draft",
  ].includes(e.status);
  return (
    <div className="container section">
      <Link href="/events" className="back">
        <ArrowLeft size={15} /> Back to all events
      </Link>
      <div className="detail-banner">
        <img src={e.image} alt={`${e.name} event setting`} />
        <div>
          <Badge status={e.status} />
          <span className="eyebrow">
            {[e.category, e.location].filter(Boolean).join(" · ")}
          </span>
          <h1>{e.name}</h1>
          <p>{e.subtitle}</p>
        </div>
      </div>
      <div className="two-column">
        <div>
          <div className="event-facts">
            <span>
              <CalendarDays />
              {dateLabel(e.date)}
              <small>{e.time} SGT</small>
            </span>
            <span>
              <MapPin />
              {e.venue}
              <small>{e.location}</small>
            </span>
          </div>
          <h2>Some evenings stay with you.</h2>
          <p className="body-copy">{e.description}</p>
          {e.organizer && (
            <div className="organizer-byline">
              <span className="avatar">
                <Ticket size={20} />
              </span>
              <div>
                <small>CURATED BY</small>
                <b>{e.organizer}</b>
              </div>
              <ShieldCheck size={20} />
            </div>
          )}
          <h3>Before you arrive</h3>
          <ul className="info-list">
            <li>Doors open 45 minutes before the event starts.</li>
            <li>Present your digital ticket and a photo ID at entry.</li>
            <li>
              Seats are automatically assigned within your chosen category.
            </li>
            <li>Maximum 5 tickets per booking. All prices are in SGD.</li>
            <li>
              {demo
                ? "This is a frontend demo; tickets and payments are simulated."
                : "Payments run in Stripe test mode, so no real charges are made."}
            </li>
          </ul>
        </div>
        <aside className="panel booking-panel event-booking">
          <span className="eyebrow">YOUR INVITATION AWAITS</span>
          <h2>Be part of the moment.</h2>
          <div className="price-large">
            {e.categories.length ? (
              <>
                {money(e.price)} <small>onwards</small>
              </>
            ) : (
              "Prices to be announced"
            )}
          </div>
          {e.categories.map((c) => (
            <div className="price-row" key={c.id}>
              <span>
                {c.name}
                <small>
                  {c.available ? `${c.available} available` : "Sold out"}
                </small>
              </span>
              <b>{money(c.price)}</b>
            </div>
          ))}
          {e.status === "Upcoming sale" && (
            <div className="notice">
              <Clock3 size={18} />
              <div>
                Sale opens {singaporeDateTime(e.release)}
                <ReleaseCountdown release={e.release} />
              </div>
            </div>
          )}
          <Button disabled={disabled} asChild={!disabled}>
            {disabled ? (
              <span>{e.status}</span>
            ) : (
              <Link
                href={
                  e.waitingRoom ? `/queue/${e.id}` : `/events/${e.id}/tickets`
                }
              >
                {e.waitingRoom ? "Join Waiting Room" : "Select Tickets"}{" "}
                <ArrowRight size={16} />
              </Link>
            )}
          </Button>
          <p className="muted small">
            <ShieldCheck size={14} /> Fair access. Secure reservations.
          </p>
        </aside>
      </div>
    </div>
  );
}
