/* eslint-disable react-hooks/set-state-in-effect -- Synchronize browser-only storage and navigation state after hydration. */
"use client";

import { Button } from "@/components/ui/button";
import { Badge, PageHeading } from "@/components/ui/shared";
import { dateLabel } from "@/lib/format";
import { type Event } from "@/types/ticketing";
import {
  ArrowRight,
  CheckCircle2,
  MoveRight,
  ShieldCheck,
  Ticket,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

const demoQueueLength = 248;

// Simulated waiting room: the position counts down in the browser.
export function Queue({
  event,
  admitted,
  admit,
}: {
  event: Event;
  admitted: boolean;
  admit: () => void;
}) {
  const [position, setPosition] = useState(demoQueueLength);
  useEffect(() => {
    try {
      const stored = localStorage.getItem(`ticketto-queue-${event.id}`);
      if (stored) setPosition(Number(stored));
    } catch {}
  }, [event.id]);
  useEffect(() => {
    try {
      localStorage.setItem(`ticketto-queue-${event.id}`, String(position));
    } catch {}
  }, [position, event.id]);
  useEffect(() => {
    if (admitted) return;
    const timer = setInterval(
      () => setPosition((p) => Math.max(0, p - 17)),
      2200,
    );
    return () => clearInterval(timer);
  }, [admitted]);
  useEffect(() => {
    if (position === 0 && !admitted) admit();
  }, [position, admitted, admit]);
  return (
    <QueueView
      event={event}
      admitted={admitted}
      position={position}
      progress={(demoQueueLength - position) / demoQueueLength}
      waitText="Estimated wait: less than a minute"
      speedUp={admit}
    />
  );
}

export function QueueView({
  event,
  admitted,
  position,
  progress,
  waitText,
  speedUp,
}: {
  event: Event;
  admitted: boolean;
  // null while the queue has not started admitting and positions are not assigned.
  position: number | null;
  progress?: number;
  waitText: string;
  speedUp?: () => void;
}) {
  return (
    <div className="container section narrow">
      <PageHeading
        label="A FAIRER WAY IN"
        title={
          admitted ? "The gate is open." : "Good things are worth the wait."
        }
        text={
          admitted
            ? "You may now select your tickets."
            : "Your place in the queue is secured. Take a breath — we’ll take it from here."
        }
      />
      <div className="panel queue-panel">
        <div className={`gateway ${admitted ? "admitted" : ""}`}>
          <Ticket size={38} />
        </div>
        <Badge status={admitted ? "Admitted" : "In the waiting room"} />
        <h2>{event.name}</h2>
        <p>
          {dateLabel(event.date)} · {event.venue}
        </p>
        {admitted ? (
          <>
            <CheckCircle2 className="success-icon" />
            <Button asChild>
              <Link href={`/events/${event.id}/tickets`}>
                Select Tickets <ArrowRight size={17} />
              </Link>
            </Button>
          </>
        ) : (
          <>
            <span className="eyebrow">YOUR QUEUE POSITION</span>
            <div className="queue-number">
              {position === null ? "—" : position.toLocaleString()}
            </div>
            {progress !== undefined && (
              <div
                className="progress"
                role="progressbar"
                aria-label="Queue progress"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(progress * 100)}
              >
                <span style={{ width: `${progress * 100}%` }} />
              </div>
            )}
            <p>{waitText}</p>
            <div className="notice">
              <ShieldCheck size={20} />
              Your spot is protected. Refreshing won’t improve your position.
            </div>
            {speedUp && (
              <Button variant="ghost" onClick={speedUp}>
                Demo: speed up admission <MoveRight size={16} />
              </Button>
            )}
          </>
        )}
      </div>
      {speedUp && (
        <p className="center muted small">
          This waiting room is a simulation for demonstration purposes.
        </p>
      )}
    </div>
  );
}
