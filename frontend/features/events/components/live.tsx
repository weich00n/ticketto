"use client";
// The public event page with live ticket counts from the Reservation Service.

import {
  eventAvailability,
  withAvailability,
} from "@/features/booking/reservations-api";
import { useRemote } from "@/lib/api/use-remote";
import type { Event } from "@/types/ticketing";
import { EventDetail } from "./event-detail";

export function LiveEventDetail({ event }: { event: Event }) {
  // The page shows at once with the event's totals; the live counts replace
  // them when they arrive, and stay if the Reservation Service cannot be reached.
  const { state } = useRemote(`event-availability-${event.id}`, () =>
    eventAvailability(event.id),
  );
  return (
    <EventDetail
      event={
        state.status === "ready" ? withAvailability(event, state.data) : event
      }
    />
  );
}
