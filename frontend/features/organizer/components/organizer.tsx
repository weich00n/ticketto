"use client";
/* eslint-disable @next/next/no-img-element */
import { Button } from "@/components/ui/button";
import { Badge, PageHeading } from "@/components/ui/shared";
import { dateLabel, money } from "@/lib/format";
import { type Event, type Order } from "@/types/ticketing";
import {
  ArrowUpRight,
  CalendarDays,
  LayoutDashboard,
  Plus,
  Ticket,
  TrendingUp,
} from "lucide-react";
import Link from "next/link";

export function Organizer({
  events,
  orders,
  simulated = true,
}: {
  events: Event[];
  orders: Order[];
  // Sales figures exist only for demo bookings so far.
  simulated?: boolean;
}) {
  const sold = orders.reduce((n, o) => n + o.reservation.quantity, 0);
  const revenue = orders.reduce((n, o) => n + o.total, 0);
  return (
    <div className="container section">
      <div className="section-heading">
        <PageHeading
          label="THE PEOPLE BEHIND THE MOMENTS"
          title="Organizer Dashboard"
          text="A little vision. An extraordinary experience."
        />
        <Button asChild>
          <Link href="/organizer/events/new">
            <Plus size={17} />
            Create Event
          </Link>
        </Button>
      </div>
      <div className="metrics-grid">
        {[
          {
            label: "Published events",
            value: events.filter(
              (e) =>
                !["Draft", "Event cancelled", "Event ended"].includes(e.status),
            ).length,
            icon: CalendarDays,
          },
          ...(simulated
            ? [
                { label: "Tickets sold", value: sold, icon: Ticket },
                {
                  label: "Demo revenue",
                  value: money(revenue),
                  icon: TrendingUp,
                },
              ]
            : []),
          {
            label: "Draft events",
            value: events.filter((e) => e.status === "Draft").length,
            icon: LayoutDashboard,
          },
        ].map((m) => (
          <div className="panel metric" key={m.label}>
            <span>
              {m.label}
              <m.icon size={19} />
            </span>
            <b>{m.value}</b>
            <small>
              {simulated ? "Current demo session" : "Your organiser account"}
            </small>
          </div>
        ))}
      </div>
      <div className="panel table-panel">
        <div className="section-heading">
          <h2>Your events</h2>
          <span className="muted small">{events.length} experiences</span>
        </div>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Event</th>
                <th>Date</th>
                <th>Status</th>
                {simulated && <th>Tickets sold</th>}
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.id}>
                  <td>
                    <Link
                      className="table-event"
                      href={`/organizer/events/${e.id}`}
                    >
                      <img src={e.image} alt="" />
                      <span>
                        <b>{e.name}</b>
                        <small>{e.venue}</small>
                      </span>
                    </Link>
                  </td>
                  <td>{dateLabel(e.date)}</td>
                  <td>
                    <Badge status={e.status} />
                  </td>
                  {simulated && (
                    <td>
                      {orders
                        .filter((o) => o.reservation.eventId === e.id)
                        .reduce((n, o) => n + o.reservation.quantity, 0)}
                    </td>
                  )}
                  <td>
                    <div className="table-actions">
                      <Link href={`/organizer/events/${e.id}`}>
                        Manage <ArrowUpRight size={14} />
                      </Link>
                      {/* The Event Service only accepts changes to drafts. */}
                      {(simulated || e.status === "Draft") && (
                        <Link href={`/organizer/events/${e.id}/edit`}>
                          Edit
                        </Link>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      {simulated && (
        <div className="panel recent-sales">
          <h2>Recent sales activity</h2>
          {orders.length ? (
            orders
              .slice()
              .reverse()
              .slice(0, 5)
              .map((o) => (
                <div className="price-row" key={o.id}>
                  <span>
                    <b>{o.buyer}</b>
                    <small>
                      {events.find((e) => e.id === o.reservation.eventId)?.name}{" "}
                      · {o.reservation.quantity} tickets
                    </small>
                  </span>
                  <b>{money(o.total)}</b>
                </div>
              ))
          ) : (
            <p className="muted">
              Your first story is waiting to begin. Completed demo purchases
              will appear here.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
