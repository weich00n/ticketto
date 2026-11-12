import { expect, test } from "@playwright/test";
import {
  createTestUser,
  deleteTestUser,
  type TestUser,
} from "./helpers/cognito";
import {
  cancelEventsTitled,
  cleanUp,
  formDate,
  formTime,
  signIn,
  testPng,
} from "./helpers/app";
import { availability } from "./helpers/purchase";

// Scenario 1: an organiser creates an event with an image and publishes it.
test("organiser creates and publishes an event with an image", async ({
  page,
}) => {
  const organiser: TestUser = await createTestUser("organiser");
  const title = `E2E Eras Tour ${Date.now()}`;
  try {
    await signIn(page, organiser);

    await page.goto("/organizer");
    await page.getByRole("button", { name: "Become an organiser" }).click();
    await expect(
      page
        .getByRole("link", { name: /create event|new event|create/i })
        .first(),
    ).toBeVisible();

    await page.goto("/organizer/events/new");
    await page.getByLabel("Event name").fill(title);
    await page
      .getByLabel("Event description")
      .fill("Created by the end-to-end test. Safe to ignore.");
    await page.getByLabel("Banner image").setInputFiles({
      name: "banner.png",
      mimeType: "image/png",
      buffer: testPng,
    });
    await expect(page.getByAltText("Banner preview")).toBeVisible();
    await page
      .getByLabel("Event date")
      .fill(formDate(new Date(Date.now() + 2 * 864e5)));
    await page
      .getByLabel("Venue in Singapore")
      .fill("National Stadium, Singapore");
    await page.getByLabel("Category").selectOption("Theatre");

    // Publishing needs the release at least 15 minutes away. A release 5 minutes
    // away is refused on the form, with the reason, and nothing is saved.
    const soon = new Date(Date.now() + 5 * 60_000);
    await page.getByLabel("Ticket release date").fill(formDate(soon));
    await page
      .getByLabel("Ticket release time")
      .selectOption(formTime(soon, "down"));
    await page.getByRole("button", { name: "Publish Event" }).click();
    await expect(page.locator(".field-error")).toContainText(
      "at least 15 minutes from now",
    );
    await expect(page).toHaveURL(/\/organizer\/events\/new$/);

    // The button picks the first time that is allowed.
    await page.getByRole("button", { name: "Use the earliest time" }).click();
    await page.getByRole("button", { name: "Publish Event" }).click();
    await expect(page).toHaveURL(/\/organizer$/);

    // The event is public and has its own image.
    const listed = await page.request.get(
      `/api/proxy/api/v1/events?search=${encodeURIComponent(title)}`,
    );
    const { items } = await listed.json();
    expect(items).toHaveLength(1);
    const eventId: string = items[0].event_id;
    expect(items[0].status).toBe("PUBLISHED");
    // The category chosen on the form was kept.
    expect(items[0].category).toBe("Theatre");
    expect(items[0].image_url).toContain(`/events/${eventId}/image?v=1`);

    const image = await page.request.get(`/api/proxy${items[0].image_url}`);
    expect(image.status()).toBe(200);
    expect(image.headers()["content-type"]).toBe("image/png");

    // Reservation sets up the event's stock from the EventPublished message.
    await expect
      .poll(
        async () => (await availability(page, eventId))?.categories.length,
        {
          timeout: 120_000,
          message: "Reservation should know the published event",
        },
      )
      .toBeGreaterThan(0);
    expect((await availability(page, eventId))?.status).toBe("ON_SALE");

    // It shows on the events page with that image.
    await page.goto("/events");
    await expect(page.getByText(title).first()).toBeVisible();
    await expect(
      page.locator(`img[src*="/events/${eventId}/image"]`).first(),
    ).toBeVisible();
  } finally {
    await cleanUp(
      () => cancelEventsTitled(page, title),
      () => deleteTestUser(organiser),
    );
  }
});
