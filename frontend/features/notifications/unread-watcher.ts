import { ApiError } from "@/lib/api/client";

// Keeps the unread count current. Only the latest request may change the count,
// so a slow answer can't bring back a badge the user has just cleared.
export function unreadWatcher(
  load: () => Promise<number>,
  onChange: (unread: number) => void,
) {
  let latest = 0;
  let sessionEnded = false;

  return {
    async check() {
      if (sessionEnded) return;
      latest += 1;
      const request = latest;
      try {
        const unread = await load();
        if (request === latest) onChange(unread);
      } catch (error) {
        // Any other failure keeps the last count until the next check works.
        if (error instanceof ApiError && error.status === 401)
          sessionEnded = true;
      }
    },
    clear() {
      latest += 1;
      onChange(0);
    },
  };
}
