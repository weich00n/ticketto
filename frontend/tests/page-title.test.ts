import { describe, expect, it } from "vitest";
import { pageTitle } from "@/lib/page-title";

describe("pageTitle", () => {
  it.each([
    ["/events", "Browse events"],
    ["/events/abc/tickets", "Choose your tickets"],
    ["/queue/abc", "Waiting room"],
    ["/checkout", "Checkout"],
    ["/checkout/success", "Booking confirmed"],
    ["/tickets", "My tickets"],
    ["/tickets/abc", "Your ticket"],
    ["/organizer", "Organiser dashboard"],
    ["/organizer/events/new", "Create an event"],
    ["/organizer/events/abc/edit", "Edit event"],
    ["/organizer/events/abc", "Manage event"],
  ])("names %s", (path, title) => {
    expect(pageTitle(path)).toBe(title);
  });

  it("leaves the home page and an event's own page to the default and the event name", () => {
    expect(pageTitle("/")).toBeUndefined();
    expect(pageTitle("/events/abc")).toBeUndefined();
    expect(pageTitle("/somewhere-else")).toBeUndefined();
  });
});
