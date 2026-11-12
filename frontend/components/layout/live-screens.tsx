"use client";
import { Empty } from "@/components/ui/shared";
import { LiveEventDetail } from "@/features/events/components/live";
import {
  LiveCheckout,
  LiveQueue,
  LiveSelection,
  LiveSuccess,
  LiveTickets,
  LiveTicketView,
} from "@/features/booking/components/live";
import {
  LiveEventForm,
  LiveOrganizer,
  LiveOrganizerDetail,
} from "@/features/organizer/components/live";
import type { SessionUser } from "@/lib/auth/tokens";
import type { Event } from "@/types/ticketing";
import type { ReactNode } from "react";

// Screens that read and change data through the backend services. Returns
// undefined for paths whose screen is the same with demo and service data.
export function liveScreen({
  path,
  event,
  events,
  user,
  notify,
  stripeKey,
}: {
  path: string;
  event?: Event;
  events: Event[];
  user: SessionUser | null;
  notify: (message: string) => void;
  stripeKey: string;
}): ReactNode {
  const segments = path.split("/");
  const notFound = <Empty title="Event not found" />;
  if (path.startsWith("/queue/"))
    return event ? <LiveQueue key={event.id} event={event} /> : notFound;
  if (path.startsWith("/events/") && path.endsWith("/tickets"))
    return event ? <LiveSelection key={event.id} event={event} /> : notFound;
  if (path.startsWith("/events/"))
    return event ? <LiveEventDetail key={event.id} event={event} /> : notFound;
  if (path === "/checkout/success") return <LiveSuccess events={events} />;
  if (path === "/checkout")
    return <LiveCheckout events={events} stripeKey={stripeKey} />;
  if (path === "/tickets") return <LiveTickets user={user} />;
  if (path.startsWith("/tickets/"))
    return (
      <LiveTicketView key={segments[2]} ticketId={segments[2]} user={user} />
    );
  if (path === "/organizer/events/new")
    return <LiveEventForm key="new" notify={notify} />;
  if (path.startsWith("/organizer/events/") && path.endsWith("/edit"))
    return <LiveEventForm key={path} eventId={segments[3]} notify={notify} />;
  if (path.startsWith("/organizer/events/"))
    return (
      <LiveOrganizerDetail
        key={segments[3]}
        eventId={segments[3]}
        notify={notify}
      />
    );
  if (path === "/organizer") return <LiveOrganizer />;
  return undefined;
}
