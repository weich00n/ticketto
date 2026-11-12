import { expect, type Page } from "@playwright/test";

export type Event = { event_id: string; release: Date };

// Creates and publishes an event through the API as the signed-in organiser.
export async function publishEvent(
  page: Page,
  title: string,
  minutesUntilRelease: number,
  image: Buffer,
): Promise<Event> {
  const release = new Date(Date.now() + minutesUntilRelease * 60_000);
  const iso = (date: Date) => date.toISOString().replace(/\.\d{3}Z$/, "Z");
  const created = await page.request.post(
    "/api/proxy/api/v1/organizer/events",
    {
      data: {
        title,
        description: "Created by the end-to-end test. Safe to ignore.",
        venue: "National Stadium, Singapore",
        starts_at: iso(new Date(release.getTime() + 2 * 864e5)),
        ticket_release_at: iso(release),
        ticket_categories: [
          {
            name: "CAT 1",
            price_amount: 8800,
            currency: "SGD",
            total_quantity: 20,
          },
        ],
      },
    },
  );
  expect(created.status(), await created.text()).toBe(201);
  const { event_id } = await created.json();
  const uploaded = await page.request.put(
    `/api/proxy/api/v1/organizer/events/${event_id}/image`,
    { data: image, headers: { "content-type": "image/png" } },
  );
  expect(uploaded.status(), await uploaded.text()).toBe(200);
  const published = await page.request.post(
    `/api/proxy/api/v1/organizer/events/${event_id}/publish`,
  );
  expect(published.status(), await published.text()).toBe(200);
  return { event_id, release };
}

export type Availability = {
  status: "ON_SALE" | "CANCELLED";
  categories: { total: number; free: number; sold: number }[];
};

// Reservation Service's public stock figures for an event. Returns undefined
// until Reservation has heard about the event.
export async function availability(page: Page, eventId: string) {
  const response = await page.request.get(
    `/api/proxy/api/v1/events/${eventId}/availability`,
  );
  return response.ok() ? ((await response.json()) as Availability) : undefined;
}

async function payWithCard(page: Page, number: string) {
  const card = page.frameLocator('iframe[title*="Secure payment"]').first();
  await card.getByPlaceholder("1234 1234 1234 1234").fill(number);
  await card.getByPlaceholder("MM / YY").fill("12 / 34");
  await card.getByPlaceholder("CVC").fill("123");
  await page.getByRole("button", { name: /^Pay / }).click();
}

// Joins the waiting room, waits to be admitted, picks one ticket and pays with
// Stripe's test card. Resolves on the order confirmation page. With
// declinedFirst, the first payment uses a card that is declined, and the buyer
// retries on the same order with a card that works.
export async function buyOneTicket(
  page: Page,
  { event_id: eventId, release }: Event,
  { declinedFirst = false } = {},
) {
  // The waiting room opens 15 minutes before the release.
  const opensIn = release.getTime() - 15 * 60_000 - Date.now();
  if (opensIn > 0) await page.waitForTimeout(opensIn + 5_000);
  await page.goto(`/events/${eventId}`);
  await page.getByRole("link", { name: "Join Waiting Room" }).click();
  // Places are drawn at the release time, so this can take a while.
  await expect(page.getByText("The gate is open.")).toBeVisible({
    timeout: 20 * 60_000,
  });
  await page.getByRole("link", { name: "Select Tickets" }).click();
  await expect(page.getByText(/remaining/).first()).toBeVisible();
  await page.getByRole("button", { name: /Reserve Tickets/ }).click();

  await page.waitForURL(/\/checkout$/);
  await page.getByLabel("Full name").fill("E2E Buyer");
  await page.getByLabel("Email address").fill("e2e-buyer@example.com");
  await page.getByLabel("I agree to the event entry instructions.").check();
  await page.getByRole("button", { name: "Continue to payment" }).click();

  if (declinedFirst) {
    await payWithCard(page, "4000000000009995");
    await expect(page.locator("p.error-message")).toBeVisible({
      timeout: 30_000,
    });
    // The order is still open, so the buyer can try another card.
    await expect(page).toHaveURL(/\/checkout$/);
  }
  await payWithCard(page, "4242424242424242");
  await page.waitForURL(/\/checkout\/success/, { timeout: 60_000 });
}
