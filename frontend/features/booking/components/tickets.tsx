"use client";
/* eslint-disable @next/next/no-img-element */
import { Button } from "@/components/ui/button";
import { Badge, Empty, PageHeading } from "@/components/ui/shared";
import { dateLabel } from "@/lib/format";
import { type DigitalTicket, type Event } from "@/types/ticketing";
import { ArrowUpRight, Ticket } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

export function Tickets({
  tickets,
  events,
}: {
  tickets: DigitalTicket[];
  events: Event[];
}) {
  const [tab, setTab] = useState("Upcoming");
  const [now] = useState(() => Date.now());
  const datedTickets = tickets.map((t) =>
    t.status === "Valid" &&
    new Date(
      (events.find((e) => e.id === t.eventId)?.date || "2099-01-01") +
        "T23:59:00+08:00",
    ).getTime() < now
      ? { ...t, status: "Past" as const }
      : t,
  );
  const filtered = datedTickets.filter((t) =>
    tab === "Cancelled"
      ? t.status === "Cancelled"
      : tab === "Past"
        ? t.status === "Past"
        : t.status === "Valid",
  );
  return (
    <div className="container section">
      <PageHeading
        label="YOUR COLLECTION OF MOMENTS"
        title="Good memories start here."
        text="Your tickets, all in one place. Ready when you are."
      />
      <div className="category-tabs">
        {["Upcoming", "Past", "Cancelled"].map((t) => (
          <button
            key={t}
            className={tab === t ? "selected" : ""}
            onClick={() => setTab(t)}
          >
            {t}{" "}
            <span>
              {
                tickets.filter((x) =>
                  t === "Upcoming" ? x.status === "Valid" : x.status === t,
                ).length
              }
            </span>
          </button>
        ))}
      </div>
      {!filtered.length ? (
        <Empty
          title={`No ${tab.toLowerCase()} tickets yet`}
          text={
            tab === "Upcoming"
              ? "Your next unforgettable moment is waiting to be found."
              : `Your ${tab.toLowerCase()} tickets will appear here.`
          }
        />
      ) : (
        <div className="my-tickets">
          {filtered.map((t) => {
            const e = events.find((e) => e.id === t.eventId);
            return (
              <article className="owned-ticket" key={t.id}>
                <img src={e?.image} alt={e?.name || "Event"} />
                <div>
                  <Badge status={t.status} />
                  <h2>{e?.name}</h2>
                  <p>
                    {e && dateLabel(e.date)} · {e?.time}
                  </p>
                  <p>{e?.venue}</p>
                  <b>
                    {t.category} · Seat {t.seat}
                  </b>
                </div>
                <div className="owned-ticket-action">
                  <Ticket size={28} />
                  <Button asChild variant="outline">
                    <Link href={`/tickets/${t.id}`}>
                      View Ticket <ArrowUpRight size={15} />
                    </Link>
                  </Button>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
