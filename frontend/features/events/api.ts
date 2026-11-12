import { api, type Page } from "@/lib/api/client";
import type { Event, EventStatus } from "@/types/ticketing";

// Response shapes from the Event Service (GET /api/v1/events and /api/v1/events/:id).
export type ApiTicketCategory = {
  category_id: string;
  name: string;
  price_amount: number; // cents
  currency: string;
  total_quantity: number;
};

export type ApiEvent = {
  event_id: string;
  title: string;
  description: string;
  venue: string;
  // Concerts, Theatre and so on; null for events made before categories were kept.
  category?: string | null;
  starts_at: string;
  ticket_release_at: string;
  queue_join_opens_at: string;
  // Path to the banner image, or null when the organiser has not uploaded one.
  image_url?: string | null;
  // The Event Service marks a published event COMPLETED once it has started.
  status: "DRAFT" | "PUBLISHED" | "CANCELLED" | "COMPLETED";
  ticket_categories: ApiTicketCategory[];
};

export const timeZone = "Asia/Singapore";
const fallbackImage = "/concert-hall.jpg";
const maxTicketsPerOrder = 5;

export const localDate = (iso: string) =>
  new Intl.DateTimeFormat("en-CA", { timeZone }).format(new Date(iso));
export const localTime = (iso: string) =>
  new Intl.DateTimeFormat("en-GB", {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(iso));
// Singapore has no daylight saving, so a fixed offset is exact.
const singaporeOffset = "+08:00";
const localIso = (iso: string) =>
  `${localDate(iso)}T${localTime(iso)}:00${singaporeOffset}`;
// Form values ("2026-11-14", "19:30" or "2026-11-14T19:30") are Singapore time.
export const toUtcIso = (local: string) =>
  new Date(
    /(Z|[+-]\d\d:\d\d)$/i.test(local)
      ? local
      : `${local.length === 16 ? `${local}:00` : local}${singaporeOffset}`,
  ).toISOString();

// Math.min of nothing is Infinity, which would show as S$∞.
export const lowestPrice = (categories: { price: number }[]) =>
  categories.length ? Math.min(...categories.map((c) => c.price)) : 0;

function status(event: ApiEvent): EventStatus {
  if (event.status === "DRAFT") return "Draft";
  if (event.status === "CANCELLED") return "Event cancelled";
  if (event.status === "COMPLETED") return "Event ended";
  return Date.parse(event.ticket_release_at) > Date.now()
    ? "Upcoming sale"
    : "On sale";
}

// Drafts are private, so their organiser reads the image from the organiser route.
function imageSource(event: ApiEvent, owner: boolean) {
  if (!event.image_url) return fallbackImage;
  const version = event.image_url.split("?")[1];
  const path = owner
    ? `/api/v1/organizer/events/${encodeURIComponent(event.event_id)}/image${version ? `?${version}` : ""}`
    : event.image_url;
  return `/api/proxy${path}`;
}

// Fields the Event Service does not provide yet (subtitle, organiser name, live
// availability) get neutral defaults.
export function toEvent(event: ApiEvent, { owner = false } = {}): Event {
  const categories = event.ticket_categories.map((c) => ({
    id: c.category_id,
    name: c.name,
    price: c.price_amount / 100,
    available: c.total_quantity,
    benefits: "",
  }));
  return {
    id: event.event_id,
    name: event.title,
    subtitle: "",
    category: event.category ?? "",
    date: localDate(event.starts_at),
    time: localTime(event.starts_at),
    venue: event.venue,
    location: "Singapore",
    image: imageSource(event, owner),
    status: status(event),
    price: lowestPrice(categories),
    description: event.description,
    organizer: "",
    categories,
    release: localIso(event.ticket_release_at),
    maxTickets: maxTicketsPerOrder,
    waitingRoom: true,
  };
}

// Body for creating or updating an event in the Event Service.
export const toEventInput = (event: Event) => ({
  title: event.name,
  description: event.description,
  venue: event.venue,
  ...(event.category ? { category: event.category } : {}),
  starts_at: toUtcIso(`${event.date}T${event.time}`),
  ticket_release_at: toUtcIso(event.release),
  ticket_categories: event.categories
    .filter((c) => c.available > 0)
    .map((c) => ({
      name: c.name,
      price_amount: Math.round(c.price * 100),
      currency: "SGD",
      total_quantity: c.available,
    })),
});

// Stand-in for an event known only from another service's snapshot (for
// example, a ticket), with the remaining fields left neutral.
export const placeholderEvent = (
  id: string,
  snapshot: { title: string; venue: string; starts_at: string } | null,
): Event => ({
  id,
  name: snapshot?.title ?? "Event",
  subtitle: "",
  category: "",
  date: snapshot ? localDate(snapshot.starts_at) : "",
  time: snapshot ? localTime(snapshot.starts_at) : "",
  venue: snapshot?.venue ?? "",
  location: "Singapore",
  image: fallbackImage,
  status: "On sale",
  price: 0,
  description: "",
  organizer: "",
  categories: [],
  release: "",
  maxTickets: 0,
  waitingRoom: false,
});

export async function listEvents(search = "") {
  const query = new URLSearchParams({ page_size: "100" });
  if (search) query.set("search", search);
  const page = await api<Page<ApiEvent>>(`/api/v1/events?${query}`, {
    redirectOnUnauthorized: false,
  });
  return page.items.map((item) => toEvent(item));
}

// The events with these ids, for screens that only know an event from another
// service's snapshot, such as a ticket. One that cannot be loaded is left out.
export async function getEventsById(eventIds: string[]) {
  const events = await Promise.all(
    [...new Set(eventIds)].map((id) => getEvent(id).catch(() => undefined)),
  );
  return events.filter((event): event is Event => event !== undefined);
}

export async function getEvent(eventId: string) {
  const event = await api<ApiEvent>(
    `/api/v1/events/${encodeURIComponent(eventId)}`,
    { redirectOnUnauthorized: false },
  );
  return toEvent(event);
}
