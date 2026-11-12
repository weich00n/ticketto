import { describe, expect, it } from "vitest";
import { imageProblem, maxImageBytes } from "@/features/events/images";

const file = (type: string, size: number) =>
  ({ type, size }) as unknown as File;

describe("imageProblem", () => {
  it.each(["image/jpeg", "image/png", "image/webp"])("accepts %s", (type) => {
    expect(imageProblem(file(type, 1000))).toBeUndefined();
  });

  it("accepts a file of exactly the size limit", () => {
    expect(imageProblem(file("image/png", maxImageBytes))).toBeUndefined();
  });

  it("rejects other file types", () => {
    expect(imageProblem(file("image/gif", 1000))).toMatch(/JPEG, PNG or WebP/);
    expect(imageProblem(file("application/pdf", 1000))).toBeDefined();
  });

  it("rejects a file over the size limit and names its size", () => {
    const message = imageProblem(file("image/jpeg", maxImageBytes + 1));
    expect(message).toMatch(/301 KB/);
    expect(message).toMatch(/300 KB or smaller/);
  });
});
