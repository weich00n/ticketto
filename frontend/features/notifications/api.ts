import { api, type Page } from "@/lib/api/client";

// Notification Service endpoints (/api/v1/notifications). These run in the
// background, so an ended session must not send someone to the sign-in page in
// the middle of what they are doing.

export type ApiNotification = {
  notification_id: number;
  // A string, not a union: the service can add kinds before this app knows them.
  kind: string;
  title: string;
  preview: string;
  event_id: string | null;
  order_id: string | null;
  created_at: string;
  email_status: "SENT" | "PENDING" | "FAILED";
  read_at: string | null;
};

const notificationsPath = "/api/v1/notifications";
const background = { redirectOnUnauthorized: false };

export const listNotifications = () =>
  api<Page<ApiNotification>>(`${notificationsPath}?page_size=10`, background);

export const getUnreadCount = async () =>
  (
    await api<{ unread: number }>(
      `${notificationsPath}/unread-count`,
      background,
    )
  ).unread;

export const markAllRead = () =>
  api<void>(`${notificationsPath}/read-all`, {
    method: "POST",
    ...background,
  });
