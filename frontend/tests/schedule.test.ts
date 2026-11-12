import { describe, expect, it } from "vitest";
import {
  earliestRelease,
  scheduleProblem,
  serviceScheduleProblem,
} from "@/features/organizer/schedule";

// 2030-06-01 12:00 in Singapore (UTC+8) is 04:00 UTC.
const now = Date.parse("2030-06-01T04:00:00Z");
const base = { date: "2030-07-01", time: "19:30" };

describe("scheduleProblem", () => {
  it("accepts a release more than 15 minutes away", () => {
    expect(
      scheduleProblem({ ...base, release: "2030-06-01T12:30" }, true, now),
    ).toBeUndefined();
  });

  it("accepts a release exactly 15 minutes away", () => {
    expect(
      scheduleProblem({ ...base, release: "2030-06-01T12:15" }, true, now),
    ).toBeUndefined();
  });

  it("explains the 15-minute rule for a release that is too soon", () => {
    const problem = scheduleProblem(
      { ...base, release: "2030-06-01T12:14" },
      true,
      now,
    );
    expect(problem?.field).toBe("release");
    expect(problem?.message).toMatch(/at least 15 minutes from now/);
    expect(problem?.message).toMatch(/waiting room/);
  });

  it("reads the times as Singapore time whatever the device's zone", () => {
    // 12:14 in Singapore is 04:14 UTC; read as UTC it would look 8 hours away.
    expect(
      scheduleProblem({ ...base, release: "2030-06-01T12:14" }, true, now),
    ).toBeDefined();
  });

  it("lets a draft have a release inside the 15 minutes", () => {
    expect(
      scheduleProblem({ ...base, release: "2030-06-01T12:05" }, false, now),
    ).toBeUndefined();
  });

  it("still needs a draft's release to be in the future", () => {
    const problem = scheduleProblem(
      { ...base, release: "2030-06-01T11:00" },
      false,
      now,
    );
    expect(problem).toEqual({
      field: "release",
      message: "Ticket release must be in the future.",
    });
  });

  it("rejects a release at or after the start", () => {
    const problem = scheduleProblem(
      { ...base, release: "2030-07-01T19:30" },
      true,
      now,
    );
    expect(problem?.message).toBe(
      "Ticket release must be before the event begins.",
    );
  });

  it("rejects a start in the past", () => {
    const problem = scheduleProblem(
      { date: "2030-06-01", time: "11:00", release: "2030-06-01T10:00" },
      true,
      now,
    );
    expect(problem?.field).toBe("start");
  });

  it("asks for a release when none is chosen", () => {
    expect(scheduleProblem({ ...base, release: "" }, true, now)?.field).toBe(
      "release",
    );
  });
});

describe("serviceScheduleProblem", () => {
  it("turns the service's release message into the form's own", () => {
    const problem = serviceScheduleProblem({
      ticket_release_at: "Must be at least 15 minutes in the future.",
    });
    expect(problem?.field).toBe("release");
    expect(problem?.message).toMatch(/at least 15 minutes from now/);
  });

  it("passes on any other problem with the release", () => {
    expect(
      serviceScheduleProblem({ ticket_release_at: "Must be before starts_at." })
        ?.message,
    ).toBe("Ticket release: Must be before starts_at.");
  });

  it("reports a start in the past", () => {
    expect(
      serviceScheduleProblem({ starts_at: "Must be in the future." })?.field,
    ).toBe("start");
  });

  it("ignores details about other fields", () => {
    expect(serviceScheduleProblem({ ticket_categories: "x" })).toBeUndefined();
    expect(serviceScheduleProblem(undefined)).toBeUndefined();
  });
});

describe("earliestRelease", () => {
  it("is a quarter-hour that is more than 15 minutes away", () => {
    // 12:00 Singapore + 15 min + 1 min spare, rounded up: 12:30.
    expect(earliestRelease(now)).toEqual({ date: "2030-06-01", time: "12:30" });
  });

  it("passes midnight into the next day", () => {
    const lateNow = Date.parse("2030-06-01T15:50:00Z"); // 23:50 Singapore
    expect(earliestRelease(lateNow)).toEqual({
      date: "2030-06-02",
      time: "00:15",
    });
  });

  it("is never inside the lead", () => {
    const earliest = earliestRelease(now);
    expect(
      scheduleProblem(
        { ...base, release: `${earliest.date}T${earliest.time}` },
        true,
        now,
      ),
    ).toBeUndefined();
  });
});
