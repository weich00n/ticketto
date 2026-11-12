"use client";
import { ApiError } from "@/lib/api/client";
import type { Event } from "@/types/ticketing";
import { useEffect, useState } from "react";
import { getEvent, listEvents } from "./api";

type EventsState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; events: Event[] };

const errorMessage = (error: unknown) =>
  error instanceof ApiError
    ? error.message
    : "Events could not be loaded. Please try again.";

// Public event data, from the demo store or from the Event Service. The list
// is refreshed in the background on every navigation, keeping the previous
// list on screen meanwhile. `eventId` is the event in the current URL; it is
// fetched on its own when the list does not include it (for example, a
// cancelled event).
export function useEvents(
  demoData: boolean,
  demoEvents: Event[],
  path: string,
  eventId?: string,
): EventsState {
  const [state, setState] = useState<EventsState>({ status: "loading" });

  useEffect(() => {
    if (demoData) return;
    let active = true;
    listEvents()
      .then(async (events) => {
        if (eventId && !events.some((e) => e.id === eventId)) {
          const event = await getEvent(eventId).catch(() => undefined);
          if (event) events = [...events, event];
        }
        if (active) setState({ status: "ready", events });
      })
      .catch((error) => {
        // A failed refresh keeps the list already on screen.
        if (active)
          setState((current) =>
            current.status === "ready"
              ? current
              : { status: "error", message: errorMessage(error) },
          );
      });
    return () => {
      active = false;
    };
  }, [demoData, path, eventId]);

  return demoData ? { status: "ready", events: demoEvents } : state;
}
