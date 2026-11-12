import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "@/lib/api/client";

afterEach(() => vi.unstubAllGlobals());

const respondWith = (status: number, body: unknown) =>
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      }),
    ),
  );

const failure = async () =>
  (await api("/api/v1/x", { redirectOnUnauthorized: false }).catch(
    (e: unknown) => e,
  )) as ApiError;

describe("api errors", () => {
  it("keeps the service's per-field details", async () => {
    respondWith(400, {
      error: {
        code: "VALIDATION_ERROR",
        message: "The event cannot be published yet.",
        details: {
          ticket_release_at: "Must be at least 15 minutes in the future.",
        },
      },
    });
    const error = await failure();
    expect(error).toBeInstanceOf(ApiError);
    expect(error.message).toBe("The event cannot be published yet.");
    expect(error.details).toEqual({
      ticket_release_at: "Must be at least 15 minutes in the future.",
    });
  });

  it("has no details when the service sends none", async () => {
    respondWith(404, {
      error: { code: "NOT_FOUND", message: "No such event." },
    });
    expect((await failure()).details).toBeUndefined();
  });
});
