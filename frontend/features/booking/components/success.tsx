"use client";

import { BlurFade } from "@/components/ui/blur-fade";
import { Button } from "@/components/ui/button";
import { Empty } from "@/components/ui/shared";
import { dateLabel, money } from "@/lib/format";
import { type Event, type Order } from "@/types/ticketing";
import { Check, Ticket } from "lucide-react";
import Link from "next/link";

export function Success({
  order,
  events,
  simulated = true,
}: {
  order?: Order;
  events: Event[];
  simulated?: boolean;
}) {
  if (!order) return <Empty title="No completed booking yet" />;
  const event = events.find((e) => e.id === order.reservation.eventId);
  return (
    <div className="container section narrow">
      <BlurFade>
        <div className="success-head">
          <div className="success-seal">
            <Check size={34} />
          </div>
          <span className="eyebrow">ADMISSION GRANTED</span>
          <h1>
            Some memories
            <br />
            are already in the making.
          </h1>
          <p>
            {simulated
              ? "Your demo payment was successful. Your tickets are ready."
              : "Your payment was successful. Your tickets will arrive by email and in My Tickets shortly."}
          </p>
        </div>
        <div className="panel success-order">
          <span className="eyebrow">BOOKING {order.id}</span>
          <h2>{event?.name}</h2>
          <p>
            {event && dateLabel(event.date)} · {event?.venue}
          </p>
          <div className="price-row">
            <span>
              {order.reservation.category} × {order.reservation.quantity}
            </span>
            <b>{money(order.total)}</b>
          </div>
          {order.reservation.seats.length > 0 && (
            <p>
              Assigned seats: <b>{order.reservation.seats.join(", ")}</b>
            </p>
          )}
          <p className="muted">Booked for {order.buyer}</p>
          <Button asChild>
            <Link href="/tickets">
              View My Tickets <Ticket size={17} />
            </Link>
          </Button>
        </div>
      </BlurFade>
    </div>
  );
}
