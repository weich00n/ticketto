import { api } from "@/lib/api/client";

// Queue Service buyer endpoints (/api/v1/events/:id/queue). The gateway sets
// X-User-Id from the access token.

export type QueueStatus =
  "SCHEDULED" | "OPEN" | "ADMITTING" | "CLOSED" | "CANCELLED";

export type Admission = {
  admission_id: string;
  user_id: string;
  event_id: string;
  admitted_at: string;
  selection_expires_at: string;
  admission_expires_at: string;
};

export type QueueEntry = {
  event_id: string;
  queue_status: QueueStatus;
  entry_status: "WAITING" | "ADMITTED" | "NONE";
  position?: number | null;
  release_at?: string;
  admission?: Admission;
  admission_token?: string;
  poll_after_seconds: number | null;
};

const queuePath = (eventId: string) =>
  `/api/v1/events/${encodeURIComponent(eventId)}/queue`;

export const joinQueue = (eventId: string) =>
  api<QueueEntry>(`${queuePath(eventId)}/entries`, { method: "POST" });

export const queueStatus = (eventId: string) =>
  api<QueueEntry>(`${queuePath(eventId)}/me`);

export const leaveQueue = (eventId: string) =>
  api<void>(`${queuePath(eventId)}/me`, { method: "DELETE" });
