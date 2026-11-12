import { afterEach, describe, expect, it, vi } from "vitest";
import {
  hasPassed,
  toLocalClock,
  withAvailability,
  type Availability,
} from "@/features/booking/reservations-api";
import type { Event } from "@/types/ticketing";

afterEach(() => vi.useRealTimers());

describe("hasPassed", () => {
  const serverTime = "2030-01-01T10:00:00Z";

  it("is false before the deadline", () => {
    expect(hasPassed("2030-01-01T10:00:01Z", serverTime)).toBe(false);
  });

  it("is true at and after the deadline", () => {
    expect(hasPassed("2030-01-01T10:00:00Z", serverTime)).toBe(true);
    expect(hasPassed("2030-01-01T09:59:59Z", serverTime)).toBe(true);
  });
});

describe("toLocalClock", () => {
  it("keeps the server's gap even when this device's clock is wrong", () => {
    vi.useFakeTimers({ now: new Date("2020-06-06T00:00:00Z") });
    const local = toLocalClock("2030-01-01T10:05:00Z", "2030-01-01T10:00:00Z");
    expect(local - Date.now()).toBe(5 * 60 * 1000);
  });
});

describe("withAvailability", () => {
  const event = {
    categories: [
      { id: "c1", name: "CAT 1", price: 288, available: 100, benefits: "" },
      { id: "c2", name: "CAT 2", price: 128, available: 200, benefits: "" },
    ],
  } as Event;
  const live = (categories: Availability["categories"]): Availability => ({
    event_id: "evt-1",
    status: "ON_SALE",
    server_time: "2030-01-01T10:00:00Z",
    categories,
  });

  it("shows the free count instead of the capacity", () => {
    const result = withAvailability(
      event,
      live([{ category_id: "c1", total: 100, free: 97, sold: 3 }]),
    );
    expect(result.categories[0].available).toBe(97);
  });

  it("keeps the capacity of a category it does not list", () => {
    const result = withAvailability(
      event,
      live([{ category_id: "c1", total: 100, free: 97, sold: 3 }]),
    );
    expect(result.categories[1].available).toBe(200);
  });

  it("shows a sold-out category as zero", () => {
    const result = withAvailability(
      event,
      live([{ category_id: "c2", total: 200, free: 0, sold: 200 }]),
    );
    expect(result.categories[1].available).toBe(0);
  });
});
