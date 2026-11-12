import { describe, expect, it } from "vitest";
import { isTransient } from "@/features/booking/polling";
import { ApiError } from "@/lib/api/client";

describe("isTransient", () => {
  it("retries network failures", () => {
    expect(isTransient(new TypeError("Failed to fetch"))).toBe(true);
  });

  it.each([500, 502, 503, 504, 408, 429])("retries HTTP %i", (status) => {
    expect(isTransient(new ApiError(status, "try later"))).toBe(true);
  });

  it.each([400, 401, 403, 404, 409])("does not retry HTTP %i", (status) => {
    expect(isTransient(new ApiError(status, "no", "QUEUE_NOT_OPEN"))).toBe(
      false,
    );
  });
});
