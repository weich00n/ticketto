"use client";
/* eslint-disable @next/next/no-img-element */
import { Button } from "@/components/ui/button";
import { dateLabel, money } from "@/lib/format";
import { type Event } from "@/types/ticketing";
import {
  ArrowRight,
  ArrowUpRight,
  Bookmark,
  CalendarDays,
  Clock3,
  MapPin,
  Ticket,
} from "lucide-react";
import Image from "next/image";
import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";

export function Badge({ status }: { status: string }) {
  return (
    <span className={`badge ${status.toLowerCase().replaceAll(" ", "-")}`}>
      <span />
      {status}
    </span>
  );
}

export function Field({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
    </label>
  );
}

export function Timer({
  expires,
  onExpired,
}: {
  expires: number;
  onExpired?: () => void;
}) {
  const [left, setLeft] = useState(() =>
    Math.max(0, Math.floor((expires - Date.now()) / 1000)),
  );
  useEffect(() => {
    const t = setInterval(() => {
      const n = Math.max(0, Math.floor((expires - Date.now()) / 1000));
      setLeft(n);
      if (n === 0) onExpired?.();
    }, 1000);
    return () => clearInterval(t);
  }, [expires, onExpired]);
  return (
    <span className="timer">
      <Clock3 size={16} />
      {Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")} remaining
    </span>
  );
}

export function EventCard({
  event: e,
  saved,
  save,
}: {
  event: Event;
  saved: boolean;
  save: (id: string) => void;
}) {
  return (
    <article className="event-card">
      <div className="card-image">
        <Link href={`/events/${e.id}`}>
          <img src={e.image} alt={`${e.name} at ${e.venue}`} />
        </Link>
        <Badge status={e.status} />
        <button
          className={`save-button ${saved ? "saved" : ""}`}
          aria-label={saved ? `Unsave ${e.name}` : `Save ${e.name}`}
          onClick={() => save(e.id)}
        >
          <Bookmark size={17} fill={saved ? "currentColor" : "none"} />
        </button>
        {e.category && <span className="image-category">{e.category}</span>}
      </div>
      <div className="card-body">
        <Link href={`/events/${e.id}`}>
          <h3>{e.name}</h3>
        </Link>
        <p>
          <CalendarDays size={13} />
          {dateLabel(e.date)} <span>·</span> {e.time}
        </p>
        <p>
          <MapPin size={13} />
          {e.venue}
        </p>
        <div className="card-bottom">
          <span>
            {e.categories.length ? (
              <>
                From <b>{money(e.price)}</b> <small>/ person</small>
              </>
            ) : (
              "Prices to be announced"
            )}
          </span>
          <Link href={`/events/${e.id}`} aria-label={`View ${e.name}`}>
            <ArrowUpRight size={19} />
          </Link>
        </div>
      </div>
    </article>
  );
}

export function PageHeading({
  label,
  title,
  text,
}: {
  label: string;
  title: string;
  text?: string;
}) {
  return (
    <div className="page-heading">
      <span className="eyebrow">{label}</span>
      <h1>{title}</h1>
      {text && <p>{text}</p>}
    </div>
  );
}

export function Empty({
  title,
  text = "Find an experience to make your own.",
  action,
}: {
  title: string;
  text?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <Ticket size={36} />
      <h1>{title}</h1>
      <p>{text}</p>
      {action || (
        <Button asChild>
          <Link href="/events">
            Browse Events <ArrowRight size={16} />
          </Link>
        </Button>
      )}
    </div>
  );
}

export function Footer({ demo = false }: { demo?: boolean }) {
  return (
    <footer>
      <div className="container footer-main">
        <Link className="brand" href="/">
          <Image
            className="brand-logo"
            src="/Ticketto.png"
            alt="Ticketto"
            width={815}
            height={328}
          />
        </Link>
        <p>A little wonder. A lasting memory.</p>
        <div>
          <Link href="/events">Browse Events</Link>
          <Link href="/organizer">For Organizers</Link>
          <Link href="/tickets">My Tickets</Link>
        </div>
      </div>
      <div className="container footer-bottom">
        <span>© 2026 Ticketto. Made for being there.</span>
        <span>
          Singapore <span className="gold">·</span>{" "}
          {demo
            ? "Frontend demonstration · No real transactions"
            : "Payments in test mode · No real charges"}
        </span>
      </div>
    </footer>
  );
}

export function ReleaseCountdown({ release }: { release: string }) {
  const [remaining, setRemaining] = useState(0);
  useEffect(() => {
    const tick = () =>
      setRemaining(Math.max(0, new Date(release).getTime() - Date.now()));
    tick();
    const t = setInterval(tick, 1000);
    return () => clearInterval(t);
  }, [release]);
  return (
    <small>
      {Math.floor(remaining / 86400000)}d {Math.floor(remaining / 3600000) % 24}
      h {Math.floor(remaining / 60000) % 60}m until release
    </small>
  );
}

export function QRPanel({ id, src }: { id: string; src?: string }) {
  if (src)
    return (
      <div className="qr-panel">
        <img src={src} alt="Ticket QR code for entry" />
      </div>
    );
  const size = 29;
  const hash = Array.from(id).reduce((a, c) => a + c.charCodeAt(0), 0);
  function cell(x: number, y: number) {
    for (const [ox, oy] of [
      [0, 0],
      [22, 0],
      [0, 22],
    ])
      if (x >= ox && x < ox + 7 && y >= oy && y < oy + 7) {
        const dx = x - ox,
          dy = y - oy;
        return (
          dx === 0 ||
          dx === 6 ||
          dy === 0 ||
          dy === 6 ||
          (dx >= 2 && dx <= 4 && dy >= 2 && dy <= 4)
        );
      }
    return (x * 73 + y * 31 + x * y * 17 + hash) % 11 < 5;
  }
  return (
    <div className="qr-panel">
      <svg
        viewBox="-3 -3 35 35"
        role="img"
        aria-label="Demonstration QR pattern, not valid for entry"
      >
        <rect x="-3" y="-3" width="35" height="35" fill="white" />
        {Array.from({ length: size * size }, (_, i) =>
          cell(i % size, Math.floor(i / size)) ? (
            <rect
              key={i}
              x={i % size}
              y={Math.floor(i / size)}
              width="1"
              height="1"
              fill="#111"
            />
          ) : null,
        )}
      </svg>
      <span>DEMO TICKET · NOT VALID FOR ENTRY</span>
    </div>
  );
}
