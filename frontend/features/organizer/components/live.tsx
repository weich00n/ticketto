"use client";
// Organiser screens backed by the Event Service. They reuse the demo screens'
// presentation; sales figures are not shown until a service provides them.

import {
  eventAvailability,
  withAvailability,
} from "@/features/booking/reservations-api";
import { Empty } from "@/components/ui/shared";
import { errorMessage, useRemote } from "@/lib/api/use-remote";
import { useRouter } from "next/navigation";
import { useRef } from "react";
import {
  ImageUploadError,
  cancelEvent,
  getMyEvent,
  listMyEvents,
  saveEvent,
} from "../api";
import { EventForm } from "./event-form";
import { Organizer } from "./organizer";
import { OrganizerDetail } from "./organizer-detail";

const Loading = ({ title }: { title: string }) => (
  <Empty title={title} text="Opening the doors…" action={<span />} />
);

export function LiveOrganizer() {
  const { state } = useRemote("organizer-events", listMyEvents);
  if (state.status === "loading")
    return <Loading title="Loading your events…" />;
  if (state.status === "error")
    return <Empty title="Your events are unavailable" text={state.message} />;
  return <Organizer events={state.data} orders={[]} simulated={false} />;
}

export function LiveOrganizerDetail({
  eventId,
  notify,
}: {
  eventId: string;
  notify: (message: string) => void;
}) {
  const { state, reload } = useRemote(`organizer-event-${eventId}`, () =>
    getMyEvent(eventId),
  );
  // A draft has no stock in the Reservation Service yet, so its request fails
  // and the page shows the capacity alone.
  const { state: live } = useRemote(`organizer-availability-${eventId}`, () =>
    eventAvailability(eventId),
  );
  if (state.status === "loading")
    return <Loading title="Loading your event…" />;
  if (state.status === "error")
    return <Empty title="Event not found" text={state.message} />;
  const stock = live.status === "ready" ? live.data : undefined;
  return (
    <OrganizerDetail
      event={stock ? withAvailability(state.data, stock) : state.data}
      orders={[]}
      simulated={false}
      sold={
        stock &&
        Object.fromEntries(stock.categories.map((c) => [c.category_id, c.sold]))
      }
      cancel={async (id) => {
        try {
          await cancelEvent(id, "Cancelled by the organiser.");
          notify("Event cancelled. Ticket holders will be refunded.");
          reload();
        } catch (error) {
          notify(errorMessage(error, "The event could not be cancelled."));
        }
      }}
    />
  );
}

export function LiveEventForm({
  eventId,
  notify,
}: {
  eventId?: string;
  notify: (message: string) => void;
}) {
  const router = useRouter();
  // The event being edited. A new event gets its id when it is first saved, so
  // that saving again after a failure changes that event instead of making another.
  const savedId = useRef(eventId);
  const { state } = useRemote(`organizer-form-${eventId ?? "new"}`, async () =>
    eventId ? getMyEvent(eventId) : undefined,
  );
  if (state.status === "loading")
    return <Loading title="Loading your event…" />;
  if (state.status === "error")
    return <Empty title="Event not found" text={state.message} />;
  // The Event Service only accepts changes to drafts.
  if (state.data && state.data.status !== "Draft")
    return (
      <Empty
        title="Published events cannot be edited"
        text="Only drafts can be changed. Cancel the event if it can no longer go ahead."
      />
    );
  return (
    <EventForm
      event={state.data}
      saveNote="Saved to your organiser account."
      uploadImage
      save={async (event, image) => {
        try {
          const saved = await saveEvent(
            event,
            savedId.current,
            image,
            (id) => (savedId.current = id),
          );
          notify(
            saved.status === "Draft"
              ? "Draft saved."
              : "Event published. Your doors are open.",
          );
        } catch (error) {
          // The draft exists, so leave the form instead of creating a second one.
          if (!(error instanceof ImageUploadError)) throw error;
          notify(
            `Draft saved, but the image was not uploaded: ${error.message} Edit the draft to try again.`,
          );
        }
        router.push("/organizer");
      }}
    />
  );
}
