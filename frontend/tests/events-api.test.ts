import { afterEach, describe, expect, it, vi } from "vitest";
import {
  getEventsById,
  toEvent,
  toEventInput,
  toUtcIso,
  type ApiEvent,
} from "@/features/events/api";

const apiEvent = (overrides: Partial<ApiEvent> = {}): ApiEvent => ({
  event_id: "evt-1",
  title: "The Eras Tour",
  description: "A night to remember",
  venue: "National Stadium",
  starts_at: "2030-11-14T11:30:00Z",
  ticket_release_at: "2030-10-01T02:00:00Z",
  queue_join_opens_at: "2030-10-01T01:45:00Z",
  image_url: null,
  status: "PUBLISHED",
  ticket_categories: [
    {
      category_id: "c1",
      name: "CAT 1",
      price_amount: 28800,
      currency: "SGD",
      total_quantity: 100,
    },
    {
      category_id: "c2",
      name: "CAT 2",
      price_amount: 12800,
      currency: "SGD",
      total_quantity: 50,
    },
  ],
  ...overrides,
});

describe("toUtcIso", () => {
  it("reads form values as Singapore time", () => {
    expect(toUtcIso("2026-11-14T19:30")).toBe("2026-11-14T11:30:00.000Z");
  });

  it("keeps seconds when the form value has them", () => {
    expect(toUtcIso("2026-11-14T19:30:15")).toBe("2026-11-14T11:30:15.000Z");
  });

  it("leaves a value that already has an offset alone", () => {
    expect(toUtcIso("2026-11-14T19:30:00+08:00")).toBe(
      "2026-11-14T11:30:00.000Z",
    );
    expect(toUtcIso("2026-11-14T11:30:00Z")).toBe("2026-11-14T11:30:00.000Z");
  });
});

describe("toEvent", () => {
  it("shows dates and times in Singapore time", () => {
    const event = toEvent(apiEvent());
    expect(event.date).toBe("2030-11-14");
    expect(event.time).toBe("19:30");
    expect(event.release).toBe("2030-10-01T10:00:00+08:00");
  });

  it("converts cents to dollars and picks the lowest price", () => {
    const event = toEvent(apiEvent());
    expect(event.categories.map((c) => c.price)).toEqual([288, 128]);
    expect(event.price).toBe(128);
  });

  it("uses the fallback image when there is none", () => {
    expect(toEvent(apiEvent()).image).toBe("/concert-hall.jpg");
  });

  it("serves a public image through the proxy", () => {
    const event = toEvent(
      apiEvent({ image_url: "/api/v1/events/evt-1/image?v=3" }),
    );
    expect(event.image).toBe("/api/proxy/api/v1/events/evt-1/image?v=3");
  });

  it("serves a draft's image from the organiser route", () => {
    const event = toEvent(
      apiEvent({ image_url: "/api/v1/events/evt-1/image?v=3" }),
      { owner: true },
    );
    expect(event.image).toBe(
      "/api/proxy/api/v1/organizer/events/evt-1/image?v=3",
    );
  });

  it("shows the category the organiser chose", () => {
    expect(toEvent(apiEvent({ category: "Theatre" })).category).toBe("Theatre");
  });

  it("has no category for an event made before categories were kept", () => {
    expect(toEvent(apiEvent({ category: null })).category).toBe("");
    expect(toEvent(apiEvent()).category).toBe("");
  });

  it("has a price of 0, not infinity, when there are no categories", () => {
    const event = toEvent(apiEvent({ ticket_categories: [] }));
    expect(event.categories).toEqual([]);
    expect(event.price).toBe(0);
  });

  describe("status", () => {
    it.each([
      ["DRAFT", "Draft"],
      ["CANCELLED", "Event cancelled"],
      ["COMPLETED", "Event ended"],
    ] as const)("maps %s", (status, label) => {
      expect(toEvent(apiEvent({ status })).status).toBe(label);
    });

    it("is an upcoming sale before the release time", () => {
      vi.useFakeTimers({ now: new Date("2030-09-01T00:00:00Z") });
      expect(toEvent(apiEvent()).status).toBe("Upcoming sale");
      vi.useRealTimers();
    });

    it("is on sale after the release time", () => {
      vi.useFakeTimers({ now: new Date("2030-10-02T00:00:00Z") });
      expect(toEvent(apiEvent()).status).toBe("On sale");
      vi.useRealTimers();
    });
  });
});

describe("toEventInput", () => {
  it("sends cents and UTC times, and drops empty categories", () => {
    const event = toEvent(apiEvent());
    event.categories.push({
      id: "new",
      name: "Unused",
      price: 10,
      available: 0,
      benefits: "",
    });
    const input = toEventInput(event);
    expect(input.starts_at).toBe("2030-11-14T11:30:00.000Z");
    expect(input.ticket_release_at).toBe("2030-10-01T02:00:00.000Z");
    expect(input.ticket_categories).toEqual([
      {
        name: "CAT 1",
        price_amount: 28800,
        currency: "SGD",
        total_quantity: 100,
      },
      {
        name: "CAT 2",
        price_amount: 12800,
        currency: "SGD",
        total_quantity: 50,
      },
    ]);
  });
});

describe("category in what is sent", () => {
  const form = () => ({ ...toEvent(apiEvent({ category: "Sports" })) });

  it("sends the chosen category", () => {
    expect(toEventInput(form()).category).toBe("Sports");
  });

  it("leaves it out when there is none", () => {
    expect("category" in toEventInput({ ...form(), category: "" })).toBe(false);
  });
});

describe("getEventsById", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("loads each event once and leaves out one that cannot be loaded", async () => {
    const fetched: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        fetched.push(url.replace("/api/proxy", ""));
        if (url.endsWith("/missing"))
          return new Response(
            JSON.stringify({ error: { code: "NOT_FOUND", message: "No." } }),
            {
              status: 404,
              headers: { "content-type": "application/json" },
            },
          );
        return new Response(JSON.stringify(apiEvent()), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );
    const events = await getEventsById(["evt-1", "evt-1", "missing"]);
    expect(events.map((e) => e.id)).toEqual(["evt-1"]);
    expect(fetched).toEqual(["/api/v1/events/evt-1", "/api/v1/events/missing"]);
  });
});
