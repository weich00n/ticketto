import { expect, type Page } from "@playwright/test";
import { deflateSync } from "node:zlib";
import type { TestUser } from "./cognito";

export async function signIn(page: Page, user: TestUser) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(user.email);
  await page.getByLabel("Password", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).not.toHaveURL(/\/login/);
}

// A small solid-colour PNG, built here so the image upload has a real file to send.
function solidPng(size: number) {
  const crcTable = Array.from({ length: 256 }, (_, n) => {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    return c >>> 0;
  });
  const crc = (data: Buffer) => {
    let c = 0xffffffff;
    for (const byte of data) c = crcTable[(c ^ byte) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  };
  const chunk = (type: string, data: Buffer) => {
    const body = Buffer.concat([Buffer.from(type), data]);
    const length = Buffer.alloc(4);
    length.writeUInt32BE(data.length);
    const checksum = Buffer.alloc(4);
    checksum.writeUInt32BE(crc(body));
    return Buffer.concat([length, body, checksum]);
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(size, 0);
  header.writeUInt32BE(size, 4);
  header.set([8, 2, 0, 0, 0], 8);
  const row = Buffer.concat([Buffer.from([0]), Buffer.alloc(size * 3, 0x66)]);
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", header),
    chunk("IDAT", deflateSync(Buffer.concat(Array(size).fill(row)))),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

export const testPng = solidPng(16);

const singapore = (date: Date, options: Intl.DateTimeFormatOptions) =>
  new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Singapore",
    hour12: false,
    ...options,
  }).format(date);

// Values for the event form's date field and its quarter-hour time lists, in Singapore time.
export const formDate = (date: Date) =>
  singapore(date, { year: "numeric", month: "2-digit", day: "2-digit" });
export const formTime = (date: Date, round: "down" | "up" = "down") => {
  const [hour, minute] = singapore(date, { hour: "2-digit", minute: "2-digit" })
    .split(":")
    .map(Number);
  const quarter = (round === "down" ? Math.floor : Math.ceil)(minute / 15) * 15;
  const total = hour * 60 + quarter;
  return `${String(Math.floor(total / 60) % 24).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
};

// Cancels every published event with this exact title. Used in cleanup, where
// the test may have failed before it learned the event's id.
export async function cancelEventsTitled(page: Page, title: string) {
  const listed = await page.request.get(
    `/api/proxy/api/v1/events?search=${encodeURIComponent(title)}`,
  );
  if (!listed.ok()) return;
  const { items } = (await listed.json()) as {
    items: { event_id: string; title: string }[];
  };
  for (const event of items.filter((item) => item.title === title))
    await page.request.post(
      `/api/proxy/api/v1/organizer/events/${event.event_id}/cancel`,
      { data: { reason: "End-to-end test cleanup." } },
    );
}

// Runs each cleanup step even if an earlier one fails, so one problem cannot
// hide the test's own result or leave test accounts behind.
export async function cleanUp(...steps: (() => Promise<unknown>)[]) {
  for (const step of steps)
    await step().catch((error) => console.warn("Cleanup step failed:", error));
}
