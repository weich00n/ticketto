import { describe, expect, it } from "vitest";
import {
  dateLabel,
  dayAndMonth,
  money,
  singaporeDateTime,
  timeAgo,
} from "@/lib/format";

describe("money", () => {
  it("formats whole Singapore dollars", () => {
    expect(money(288)).toMatch(/288$/);
    expect(money(288)).toContain("$");
  });
});

describe("dateLabel", () => {
  it("formats a date", () => {
    expect(dateLabel("2030-11-14")).toBe("14 Nov 2030");
  });

  it("says so when the date is not set", () => {
    expect(dateLabel("")).toBe("Date to be confirmed");
  });
});

describe("timeAgo", () => {
  const now = Date.parse("2030-01-10T12:00:00Z");

  it.each([
    ["2030-01-10T11:59:30Z", "Just now"],
    ["2030-01-10T11:59:00Z", "1m ago"],
    ["2030-01-10T11:01:00Z", "59m ago"],
    ["2030-01-10T11:00:00Z", "1h ago"],
    ["2030-01-09T13:00:00Z", "23h ago"],
    ["2030-01-09T12:00:00Z", "1d ago"],
    ["2029-12-31T12:00:00Z", "10d ago"],
  ])("labels %s as %s", (date, label) => {
    expect(timeAgo(date, now)).toBe(label);
  });

  it("says just now when this device's clock is behind the server's", () => {
    expect(timeAgo("2030-01-10T12:05:00Z", now)).toBe("Just now");
  });
});

describe("money with cents", () => {
  it("shows cents when the amount has them", () => {
    expect(money(12.5)).toMatch(/12\.50$/);
    expect(money(0.99)).toMatch(/0\.99$/);
  });

  it("leaves whole dollars without cents", () => {
    expect(money(13)).not.toContain(".");
  });
});

describe("singaporeDateTime", () => {
  it("shows the time in Singapore, whatever the viewer's zone", () => {
    // 11:30 UTC is 19:30 in Singapore.
    expect(singaporeDateTime("2030-11-14T11:30:00Z")).toMatch(
      /14 Nov 2030.*7:30\s?pm SGT$/i,
    );
  });

  it("reads an offset time as the same moment", () => {
    expect(singaporeDateTime("2030-11-14T19:30:00+08:00")).toBe(
      singaporeDateTime("2030-11-14T11:30:00Z"),
    );
  });
});

describe("dayAndMonth", () => {
  it("keeps the day it was given", () => {
    expect(dayAndMonth("2030-11-14")).toEqual({ day: 14, month: "Nov" });
    expect(dayAndMonth("2030-01-01")).toEqual({ day: 1, month: "Jan" });
  });
});
