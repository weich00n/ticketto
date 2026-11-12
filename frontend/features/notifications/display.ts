import type { ApiNotification } from "./api";

// Where clicking a notification goes. Everything about an order or a ticket
// opens the buyer's tickets, since there is no orders page.
export const notificationLink = ({
  kind,
  event_id,
}: Pick<ApiNotification, "kind" | "event_id">) =>
  kind === "EVENT_PUBLISHED" && event_id
    ? `/events/${encodeURIComponent(event_id)}`
    : "/tickets";

export const badgeLabel = (unread: number) =>
  unread <= 0 ? null : unread > 9 ? "9+" : String(unread);
