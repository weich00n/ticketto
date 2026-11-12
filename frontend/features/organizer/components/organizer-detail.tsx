"use client";

import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { Badge, Empty, PageHeading } from "@/components/ui/shared";
import { dateLabel, money } from "@/lib/format";
import { type Event, type Order } from "@/types/ticketing";
import { ArrowLeft, ArrowRight, ArrowUpRight, Mail } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

export function OrganizerDetail({
  event: e,
  orders,
  cancel,
  simulated = true,
  sold: soldByCategory,
}: {
  event?: Event;
  orders: Order[];
  cancel: (id: string) => void;
  // Sales figures exist only for demo bookings so far.
  simulated?: boolean;
  // Tickets sold per category id, from the Reservation Service.
  sold?: Record<string, number>;
}) {
  const [modal, setModal] = useState(false);
  if (!e) return <Empty title="Event not found" />;
  const sales = orders.filter((o) => o.reservation.eventId === e.id);
  const showSales = simulated || soldByCategory !== undefined;
  const soldIn = (c: Event["categories"][number]) =>
    soldByCategory
      ? (soldByCategory[c.id] ?? 0)
      : sales
          .filter((o) => o.reservation.category === c.name)
          .reduce((n, o) => n + o.reservation.quantity, 0);
  const sold = e.categories.reduce((n, c) => n + soldIn(c), 0);
  const capacity = e.categories.reduce((n, c) => n + c.available, 0) + sold;
  return (
    <div className="container section">
      <Link href="/organizer" className="back">
        <ArrowLeft size={15} /> Organizer Dashboard
      </Link>
      <div className="section-heading">
        <PageHeading
          label="EVENT OVERVIEW"
          title={e.name}
          text={`${dateLabel(e.date)} · ${e.venue}`}
        />
        <Badge status={e.status} />
      </div>
      <div className="two-column">
        <div className="panel">
          <h2>Every ticket is a new story.</h2>
          <p>
            {showSales
              ? `${sold} tickets sold · ${capacity} category inventory`
              : `${capacity} tickets across all categories`}
          </p>
          {showSales && (
            <div className="progress">
              <span
                style={{
                  width: `${Math.min(100, (sold / Math.max(1, capacity)) * 100)}%`,
                }}
              />
            </div>
          )}
          <h3>Inventory by category</h3>
          {e.categories.map((c) => {
            const count = soldIn(c);
            return (
              <div className="price-row" key={c.id}>
                <span>
                  <b>{c.name}</b>
                  <small>
                    {showSales
                      ? `${count} sold · ${c.available} remaining`
                      : `${c.available} tickets`}
                  </small>
                </span>
                <b>{money(c.price)}</b>
              </div>
            );
          })}
          <p className="body-copy">{e.description}</p>
        </div>
        <aside className="panel booking-panel">
          <h2>Event management</h2>
          {/* The Event Service only accepts changes to drafts, and cancels only published events. */}
          {(simulated || e.status === "Draft") && (
            <Button asChild>
              <Link href={`/organizer/events/${e.id}/edit`}>
                Update Event <ArrowRight size={16} />
              </Link>
            </Button>
          )}
          <Button asChild variant="outline">
            <Link href={`/events/${e.id}`}>
              View public page <ArrowUpRight size={16} />
            </Link>
          </Button>
          <Button
            variant="ghost"
            disabled={
              e.status === "Event cancelled" ||
              e.status === "Event ended" ||
              (!simulated && e.status === "Draft")
            }
            onClick={() => setModal(true)}
          >
            Cancel Event
          </Button>
          {e.status === "Event cancelled" && (
            <div className="notice">
              <Mail size={18} />
              {simulated ? (
                <div>
                  Mock buyer notifications queued.
                  <p>
                    All associated demo tickets are cancelled and refunds marked
                    as requested.
                  </p>
                </div>
              ) : (
                <div>
                  This event is cancelled.
                  <p>
                    Its tickets are invalidated and buyers are refunded
                    automatically.
                  </p>
                </div>
              )}
            </div>
          )}
        </aside>
      </div>
      <ConfirmDialog
        open={modal}
        onOpenChange={setModal}
        title="Cancel this event?"
      >
        <p>
          This will cancel <b>{e.name}</b>
          {simulated
            ? " and mark its demo tickets as cancelled. Mock refund and notification requests will be recorded."
            : ". Its tickets become invalid and buyers are refunded. This cannot be undone."}
        </p>
        <div className="button-row">
          <Button variant="outline" onClick={() => setModal(false)}>
            Keep Event
          </Button>
          <Button
            onClick={() => {
              cancel(e.id);
              setModal(false);
            }}
          >
            Confirm Cancellation
          </Button>
        </div>
      </ConfirmDialog>
    </div>
  );
}
