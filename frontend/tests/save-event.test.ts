import { afterEach, describe, expect, it, vi } from "vitest";
import { PublishError, saveEvent } from "@/features/organizer/api";
import type { Event } from "@/types/ticketing";

afterEach(() => vi.unstubAllGlobals());

const apiEvent = (status = "DRAFT") => ({
  event_id: "evt-1",
  title: "Test night",
  description: "A description that is long enough.",
  venue: "Esplanade",
  starts_at: "2030-07-01T11:30:00Z",
  ticket_release_at: "2030-06-01T02:00:00Z",
  queue_join_opens_at: "2030-06-01T01:45:00Z",
  status,
  ticket_categories: [
    {
      category_id: "c1",
      name: "CAT 1",
      price_amount: 12800,
      currency: "SGD",
      total_quantity: 100,
    },
  ],
});

const form = (status: Event["status"]): Event => ({
  id: "new",
  name: "Test night",
  subtitle: "",
  category: "Concerts",
  date: "2030-07-01",
  time: "19:30",
  venue: "Esplanade",
  location: "Singapore",
  image: "/opera.jpg",
  status,
  price: 128,
  description: "A description that is long enough.",
  organizer: "",
  categories: [
    { id: "cat1", name: "CAT 1", price: 128, available: 100, benefits: "" },
  ],
  release: "2030-06-01T10:00",
  maxTickets: 5,
  waitingRoom: true,
});

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

// Replies by method and path, and records each call.
function backend(publish: () => Response) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      const call = `${init.method ?? "GET"} ${url.replace("/api/proxy", "")}`;
      calls.push(call);
      if (call.endsWith("/publish")) return publish();
      return json(call.startsWith("POST") ? 201 : 200, apiEvent());
    }),
  );
  return calls;
}

const tooSoon = () =>
  json(400, {
    error: {
      code: "VALIDATION_ERROR",
      message: "The event cannot be published yet.",
      details: {
        ticket_release_at: "Must be at least 15 minutes in the future.",
      },
    },
  });

describe("saveEvent", () => {
  it("tells the caller the event's id before it publishes", async () => {
    const calls = backend(() => json(200, apiEvent("PUBLISHED")));
    const order: string[] = [];
    await saveEvent(form("On sale"), undefined, undefined, (id) =>
      order.push(`saved ${id}`),
    );
    expect(order).toEqual(["saved evt-1"]);
    expect(calls).toEqual([
      "POST /api/v1/organizer/events",
      "POST /api/v1/organizer/events/evt-1/publish",
    ]);
  });

  it("keeps the draft when publishing fails, with the service's reason", async () => {
    backend(tooSoon);
    const failure = (await saveEvent(form("On sale")).catch(
      (e: unknown) => e,
    )) as PublishError;
    expect(failure).toBeInstanceOf(PublishError);
    expect(failure.saved.id).toBe("evt-1");
    expect(failure.details?.ticket_release_at).toMatch(/15 minutes/);
  });

  it("changes the same event when the organiser tries again", async () => {
    const calls = backend(tooSoon);
    let savedId: string | undefined;
    const attempt = () =>
      saveEvent(form("On sale"), savedId, undefined, (id) => (savedId = id));

    await attempt().catch(() => undefined);
    await attempt().catch(() => undefined);

    // One event is created; the second attempt updates it.
    expect(
      calls.filter(
        (c) =>
          c.startsWith("POST /api/v1/organizer/events") &&
          !c.endsWith("/publish"),
      ),
    ).toHaveLength(1);
    expect(calls).toContain("PUT /api/v1/organizer/events/evt-1");
  });
});
