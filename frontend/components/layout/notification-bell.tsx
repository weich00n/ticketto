"use client";
import { Bell } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { listNotifications, markAllRead } from "@/features/notifications/api";
import { badgeLabel, notificationLink } from "@/features/notifications/display";
import { useUnreadCount } from "@/features/notifications/use-unread-count";
import { useRemote } from "@/lib/api/use-remote";
import { timeAgo } from "@/lib/format";

const caughtUp = <p>You&apos;re all caught up.</p>;

// Mounted each time the dropdown opens, so it always shows the latest. Unread
// items keep their highlight until it closes, even though they are now read.
function NotificationList({
  onRead,
  onNavigate,
}: {
  onRead: () => void;
  onNavigate: () => void;
}) {
  const { state, reload } = useRemote(
    "notifications",
    async () => {
      const { items } = await listNotifications();
      // Only now has the user seen them. If this fails they just stay unread.
      if (items.some((item) => !item.read_at))
        void markAllRead().then(onRead, () => undefined);
      return items;
    },
    "Your notifications could not be loaded.",
  );

  if (state.status === "loading") return <p>Loading…</p>;
  if (state.status === "error")
    return (
      <>
        <p role="alert">{state.message}</p>
        <button className="bell-retry" onClick={reload}>
          Try again
        </button>
      </>
    );
  if (!state.data.length) return caughtUp;
  return (
    <ul className="bell-list">
      {state.data.map((item) => (
        <li key={item.notification_id}>
          <Link
            className={item.read_at ? "bell-item" : "bell-item unread"}
            href={notificationLink(item)}
            onClick={onNavigate}
          >
            <strong>{item.title}</strong>
            <span>{item.preview}</span>
            <small>{timeAgo(item.created_at)}</small>
          </Link>
        </li>
      ))}
    </ul>
  );
}

// `live` is false in demo mode, where there is no notifications API to call.
export function NotificationBell({ live }: { live: boolean }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const { unread, clearUnread } = useUnreadCount(live);
  const badge = badgeLabel(unread);

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  return (
    <div className="notification-bell" ref={ref}>
      <button
        className="bell-button"
        onClick={() => setOpen(!open)}
        aria-label={badge ? `Notifications, ${badge} unread` : "Notifications"}
        aria-haspopup="dialog"
        aria-expanded={open}
      >
        <Bell size={19} />
        {badge && (
          <span className="bell-badge" aria-hidden="true">
            {badge}
          </span>
        )}
      </button>
      {open && (
        <div className="bell-dropdown" role="dialog" aria-label="Notifications">
          <h4>Notifications</h4>
          {live ? (
            <NotificationList
              onRead={clearUnread}
              onNavigate={() => setOpen(false)}
            />
          ) : (
            caughtUp
          )}
        </div>
      )}
    </div>
  );
}
