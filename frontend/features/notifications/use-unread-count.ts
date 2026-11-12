"use client";
import { useEffect, useState } from "react";
import { getUnreadCount } from "./api";
import { unreadWatcher } from "./unread-watcher";

const checkEveryMs = 60_000;

// Checks once a minute while the tab is visible, and as soon as it becomes
// visible again. Not useRemote: that goes back to "loading" on every reload,
// which would make the badge flicker.
export function useUnreadCount(enabled: boolean) {
  const [unread, setUnread] = useState(0);
  const [watcher] = useState(() => unreadWatcher(getUnreadCount, setUnread));

  useEffect(() => {
    if (!enabled) return;
    const checkIfVisible = () => {
      if (document.visibilityState === "visible") void watcher.check();
    };
    checkIfVisible();
    const timer = setInterval(checkIfVisible, checkEveryMs);
    document.addEventListener("visibilitychange", checkIfVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", checkIfVisible);
    };
  }, [enabled, watcher]);

  return { unread, clearUnread: watcher.clear };
}
