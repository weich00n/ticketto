import { toEvent, toEventInput, type ApiEvent } from "@/features/events/api";
import { ApiError, api, type Page } from "@/lib/api/client";
import { errorMessage } from "@/lib/api/use-remote";
import type { Event } from "@/types/ticketing";

// Event Service organiser endpoints (/api/v1/organizer/events) and the User
// Service organiser upgrade.

const eventsPath = "/api/v1/organizer/events";
const eventPath = (eventId: string) =>
  `${eventsPath}/${encodeURIComponent(eventId)}`;

const asOwner = (event: ApiEvent) => toEvent(event, { owner: true });

export const listMyEvents = async () =>
  (await api<Page<ApiEvent>>(`${eventsPath}?page_size=100`)).items.map(asOwner);

export const getMyEvent = async (eventId: string) =>
  asOwner(await api<ApiEvent>(eventPath(eventId)));

// The draft was saved, but its image was not.
export class ImageUploadError extends Error {
  constructor(
    message: string,
    readonly saved: Event,
  ) {
    super(message);
    this.name = "ImageUploadError";
  }
}

// The event was saved as a draft, but could not be published.
export class PublishError extends Error {
  constructor(
    message: string,
    readonly saved: Event,
    readonly details?: Record<string, string>,
  ) {
    super(message);
    this.name = "PublishError";
  }
}

// Only drafts can be changed. A new event is created as a draft, gets its image
// when the organiser chose one, and is published straight after when they chose
// to publish. If the image is rejected the event stays a draft.
// `onSaved` is told the event's id as soon as it exists, before the image and the
// publish step, so a failure there can be retried on the same event instead of
// creating another one.
export async function saveEvent(
  event: Event,
  existingId?: string,
  image?: File,
  onSaved?: (eventId: string) => void,
) {
  const json = toEventInput(event);
  let saved = existingId
    ? await api<ApiEvent>(eventPath(existingId), { method: "PUT", json })
    : await api<ApiEvent>(eventsPath, { method: "POST", json });
  onSaved?.(saved.event_id);
  if (image) {
    try {
      saved = await api<ApiEvent>(`${eventPath(saved.event_id)}/image`, {
        method: "PUT",
        file: image,
      });
    } catch (error) {
      throw new ImageUploadError(
        errorMessage(error, "The image could not be uploaded."),
        asOwner(saved),
      );
    }
  }
  if (event.status === "Draft") return asOwner(saved);
  try {
    return asOwner(
      await api<ApiEvent>(`${eventPath(saved.event_id)}/publish`, {
        method: "POST",
      }),
    );
  } catch (error) {
    throw new PublishError(
      errorMessage(error, "The event could not be published."),
      asOwner(saved),
      error instanceof ApiError ? error.details : undefined,
    );
  }
}

export const cancelEvent = async (eventId: string, reason: string) =>
  asOwner(
    await api<ApiEvent>(`${eventPath(eventId)}/cancel`, {
      method: "POST",
      json: { reason },
    }),
  );

export const becomeOrganiser = () =>
  api<unknown>("/api/v1/users/me/organiser", { method: "POST" });
