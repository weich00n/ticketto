import { afterEach, describe, expect, it, vi } from "vitest";
import {
  getUnreadCount,
  listNotifications,
  markAllRead,
} from "@/features/notifications/api";
import { badgeLabel, notificationLink } from "@/features/notifications/display";
import { unreadWatcher } from "@/features/notifications/unread-watcher";
import { ApiError } from "@/lib/api/client";

afterEach(() => vi.unstubAllGlobals());

const respondWith = (status: number, body?: unknown) => {
  const fetchMock = vi.fn(
    async () =>
      new Response(body === undefined ? null : JSON.stringify(body), {
        status,
      }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
};

const requested = (fetchMock: ReturnType<typeof respondWith>) => {
  const [url, init] = fetchMock.mock.calls[0] as unknown as [
    string,
    RequestInit,
  ];
  return { url, method: init.method ?? "GET" };
};

describe("notifications API", () => {
  it("lists the latest ten", async () => {
    const page = { items: [], page: 1, page_size: 10, total_items: 0 };
    const fetchMock = respondWith(200, page);

    await expect(listNotifications()).resolves.toEqual(page);

    expect(requested(fetchMock)).toEqual({
      url: "/api/proxy/api/v1/notifications?page_size=10",
      method: "GET",
    });
  });

  it("reads the unread count", async () => {
    const fetchMock = respondWith(200, { unread: 3 });

    await expect(getUnreadCount()).resolves.toBe(3);

    expect(requested(fetchMock)).toEqual({
      url: "/api/proxy/api/v1/notifications/unread-count",
      method: "GET",
    });
  });

  it("marks everything read", async () => {
    const fetchMock = respondWith(204);

    await expect(markAllRead()).resolves.toBeUndefined();

    expect(requested(fetchMock)).toEqual({
      url: "/api/proxy/api/v1/notifications/read-all",
      method: "POST",
    });
  });

  it.each([
    ["listing", listNotifications],
    ["counting", getUnreadCount],
    ["marking read", markAllRead],
  ])(
    "does not send the visitor to sign in when %s gets a 401",
    async (_name, call) => {
      const assign = vi.fn();
      vi.stubGlobal("window", {
        location: { assign, pathname: "/checkout", search: "" },
      });
      respondWith(401, {
        error: { code: "UNAUTHORIZED", message: "Sign in." },
      });

      await expect(call()).rejects.toMatchObject({ status: 401 });

      expect(assign).not.toHaveBeenCalled();
    },
  );
});

describe("notificationLink", () => {
  it("opens the event for a new-event notification", () => {
    expect(
      notificationLink({ kind: "EVENT_PUBLISHED", event_id: "evt-1" }),
    ).toBe("/events/evt-1");
  });

  it.each([
    "TICKET_ISSUED",
    "EVENT_CANCELLED",
    "ORDER_REFUNDED",
    "REFUND_FAILED",
  ])("opens the buyer's tickets for %s", (kind) => {
    expect(notificationLink({ kind, event_id: "evt-1" })).toBe("/tickets");
  });

  it("falls back to the tickets for a kind it does not know", () => {
    expect(notificationLink({ kind: "SOMETHING_NEW", event_id: null })).toBe(
      "/tickets",
    );
  });

  it("falls back to the tickets when a new-event row has no event", () => {
    expect(notificationLink({ kind: "EVENT_PUBLISHED", event_id: null })).toBe(
      "/tickets",
    );
  });
});

describe("badgeLabel", () => {
  it("is hidden when nothing is unread", () => {
    expect(badgeLabel(0)).toBeNull();
  });

  it.each([
    [1, "1"],
    [9, "9"],
    [10, "9+"],
    [250, "9+"],
  ])("shows %i as %s", (count, label) => {
    expect(badgeLabel(count)).toBe(label);
  });
});

describe("unreadWatcher", () => {
  const counts = () => {
    const seen: number[] = [];
    return { seen, onChange: (unread: number) => seen.push(unread) };
  };

  it("reports the count it loads", async () => {
    const { seen, onChange } = counts();
    await unreadWatcher(async () => 3, onChange).check();
    expect(seen).toEqual([3]);
  });

  it.each([
    ["a dropped connection", new ApiError(0, "offline")],
    ["a server error", new ApiError(503, "busy")],
    ["being rate limited", new ApiError(429, "slow down")],
    ["an unexpected failure", new TypeError("Failed to fetch")],
  ])("keeps the last count and keeps checking after %s", async (_n, error) => {
    const { seen, onChange } = counts();
    const load = vi
      .fn<() => Promise<number>>()
      .mockResolvedValueOnce(3)
      .mockRejectedValueOnce(error)
      .mockResolvedValueOnce(4);
    const watcher = unreadWatcher(load, onChange);

    await watcher.check();
    await watcher.check();
    await watcher.check();

    expect(seen).toEqual([3, 4]);
    expect(load).toHaveBeenCalledTimes(3);
  });

  it("stops checking once the session has ended", async () => {
    const load = vi
      .fn<() => Promise<number>>()
      .mockRejectedValue(new ApiError(401, "Sign in."));
    const watcher = unreadWatcher(load, () => undefined);

    await watcher.check();
    await watcher.check();

    expect(load).toHaveBeenCalledTimes(1);
  });

  it("clears the count", () => {
    const { seen, onChange } = counts();
    unreadWatcher(async () => 3, onChange).clear();
    expect(seen).toEqual([0]);
  });

  it("ignores a count requested before everything was marked read", async () => {
    const { seen, onChange } = counts();
    let answer: (unread: number) => void = () => undefined;
    const load = () =>
      new Promise<number>((resolve) => {
        answer = resolve;
      });
    const watcher = unreadWatcher(load, onChange);

    const inFlight = watcher.check();
    watcher.clear();
    answer(5);
    await inFlight;

    expect(seen).toEqual([0]);
  });

  it("ignores an older answer that arrives after a newer one", async () => {
    const { seen, onChange } = counts();
    const answers: ((unread: number) => void)[] = [];
    const load = () =>
      new Promise<number>((resolve) => {
        answers.push(resolve);
      });
    const watcher = unreadWatcher(load, onChange);

    const older = watcher.check();
    const newer = watcher.check();
    answers[1](2);
    await newer;
    answers[0](7);
    await older;

    expect(seen).toEqual([2]);
  });
});
