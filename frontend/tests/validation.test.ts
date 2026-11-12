import { describe, expect, it } from "vitest";
import { safeRedirectPath } from "@/lib/auth/validation";

describe("safeRedirectPath", () => {
  it("keeps same-site paths", () => {
    expect(safeRedirectPath("/tickets")).toBe("/tickets");
    expect(safeRedirectPath("/events/1?tab=2")).toBe("/events/1?tab=2");
  });

  it.each([
    "//evil.example",
    "/\\evil.example",
    "https://evil.example",
    "evil.example",
    "",
    undefined,
    42,
  ])("rejects %j", (value) => {
    expect(safeRedirectPath(value)).toBeUndefined();
  });

  // Browsers drop tabs and newlines from a URL, so "/\t/evil.example" is "//evil.example".
  it.each([
    "/\t/evil.example",
    "/\n/evil.example",
    "/\r/evil.example",
    "/\u0000/x",
  ])("rejects a control character: %j", (value) => {
    expect(safeRedirectPath(value)).toBeUndefined();
  });
});
