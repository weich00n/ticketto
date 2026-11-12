import { api } from "@/lib/api/client";
import type { Event } from "@/types/ticketing";

// Reservation Service (/api/v1/reservations and /api/v1/events/:id/availability).
// Amounts are in cents.

export type HoldStatus = "ACTIVE" | "CONFIRMED" | "EXPIRED" | "CANCELLED";

export type Hold = {
  hold_id: string;
  admission_id: string;
  status: HoldStatus;
  quantity: number;
  unit_amount: number;
  total_amount: number;
  currency: string;
  payment_expires_at: string;
  hold_expires_at: string;
  server_time: string;
};

export type Availability = {
  event_id: string;
  status: "ON_SALE" | "CANCELLED";
  server_time: string;
  categories: {
    category_id: string;
    total: number;
    free: number;
    sold: number;
  }[];
};

export const eventAvailability = (eventId: string) =>
  api<Availability>(
    `/api/v1/events/${encodeURIComponent(eventId)}/availability`,
    { redirectOnUnauthorized: false },
  );

// The event with each category's `available` set to the Reservation Service's
// live free count. A category it does not list keeps the event's own total.
export const withAvailability = (event: Event, live: Availability): Event => ({
  ...event,
  categories: event.categories.map((c) => ({
    ...c,
    available:
      live.categories.find((a) => a.category_id === c.id)?.free ?? c.available,
  })),
});

export const createHold = (hold: {
  eventId: string;
  categoryId: string;
  quantity: number;
  admissionToken: string;
}) =>
  api<Hold>("/api/v1/reservations", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    json: {
      event_id: hold.eventId,
      category_id: hold.categoryId,
      quantity: hold.quantity,
      admission_token: hold.admissionToken,
    },
  });

export const getHold = (holdId: string) =>
  api<Hold>(`/api/v1/reservations/${encodeURIComponent(holdId)}`);

// Converts a server timestamp to this browser's clock, so countdowns stay
// right even when the device clock is off.
export const toLocalClock = (iso: string, serverTime: string) =>
  Date.parse(iso) - Date.parse(serverTime) + Date.now();

// Whether a server time has passed, judged by the server's clock.
export const hasPassed = (iso: string, serverTime: string) =>
  Date.parse(iso) <= Date.parse(serverTime);
