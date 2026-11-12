"use client";

import { Button } from "@/components/ui/button";
import { Badge, Empty, QRPanel } from "@/components/ui/shared";
import { dateLabel } from "@/lib/format";
import { type DigitalTicket, type Event } from "@/types/ticketing";
import { ArrowLeft, Download, Ticket } from "lucide-react";
import Link from "next/link";

export function TicketView({
  ticket: t,
  events,
}: {
  ticket?: DigitalTicket;
  events: Event[];
}) {
  if (!t)
    return (
      <Empty
        title="Ticket not found"
        text="Complete a demo booking to receive a digital ticket."
      />
    );
  const e = events.find((e) => e.id === t.eventId);
  return (
    <div className="container section narrow">
      <Link className="back" href="/tickets">
        <ArrowLeft size={15} /> My Tickets
      </Link>
      <div className="digital-ticket">
        <div className="digital-top">
          <Ticket size={27} />
          <span className="eyebrow">TICKETTO · YOUR INVITATION</span>
          <Badge status={t.status} />
        </div>
        <h1>{e?.name}</h1>
        <p className="center">
          {e && dateLabel(e.date)} · {e?.time} SGT
          <br />
          {e?.venue}
        </p>
        <div className="ticket-details">
          <div>
            <small>GUEST</small>
            <b>{t.buyer}</b>
          </div>
          <div>
            <small>CATEGORY</small>
            <b>{t.category}</b>
          </div>
          <div>
            <small>SEAT</small>
            <b>{t.seat}</b>
          </div>
        </div>
        <QRPanel id={t.id} src={t.qrImage} />
        <p className="ticket-id">{t.id}</p>
        <div className="ticket-instructions">
          <h3>
            {t.status === "Cancelled"
              ? "This ticket has been cancelled"
              : "An evening to remember awaits."}
          </h3>
          <p>
            {t.status === "Cancelled"
              ? "This ticket is no longer valid. Any refund is returned to your original payment method."
              : "Arrive 45 minutes early. Present your ticket and photo ID at the door. Each ticket admits one guest."}
          </p>
        </div>
      </div>
      <div className="center print-action">
        <Button variant="outline" onClick={() => window.print()}>
          <Download size={16} />
          Print / Save ticket
        </Button>
      </div>
    </div>
  );
}
