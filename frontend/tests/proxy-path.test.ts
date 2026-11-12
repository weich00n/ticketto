import { describe, expect, it } from "vitest";
import { isSafeProxyPath } from "@/lib/api/proxy-path";

describe("isSafeProxyPath", () => {
  it("allows normal API paths", () => {
    expect(isSafeProxyPath(["api", "v1", "events"])).toBe(true);
    expect(isSafeProxyPath(["api", "v1", "events", "a-b_c.1", "image"])).toBe(
      true,
    );
  });

  it.each([
    [[".."]],
    [["api", "..", "admin"]],
    [["api", "v1", ".", "events"]],
    [["api", "v1", "events", ".."]],
    [["api", "v1\\..\\x"]],
    [["api", "a/b"]],
    [["api", "x\u0000"]],
    [[]],
    [[""]],
  ])("rejects %j", (segments) => {
    expect(isSafeProxyPath(segments)).toBe(false);
  });
});
