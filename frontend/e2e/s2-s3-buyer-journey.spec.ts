import { expect, test } from "@playwright/test";
import { cancelEventsTitled, cleanUp, signIn, testPng } from "./helpers/app";
import { createTestUser, deleteTestUser } from "./helpers/cognito";
import { availability, buyOneTicket, publishEvent } from "./helpers/purchase";

type Ticket = {
  ticket_id: string;
  status: "VALID" | "CHECKED_IN" | "INVALIDATED";
  qr_payload: string | null;
};
type Order = { event_id: string; status: string; refunds: unknown[] };

// Scenario 2: a buyer queues, holds, pays with Stripe and gets a ticket that
// staff check in once. Scenario 3: the organiser cancels an event and the
// buyer's ticket is invalidated and refunded. One run covers both, because the
// queue only admits buyers at the release time, which is at least 15 minutes
// after an event is published.
test("buyer purchases tickets; cancelling an event refunds them", async ({
  page: buyerPage,
  browser,
  baseURL,
}) => {
  test.setTimeout(45 * 60_000);
  const organiser = await createTestUser("organiser");
  const buyer = await createTestUser("buyer");
  const organiserContext = await browser.newContext({ baseURL });
  const organiserPage = await organiserContext.newPage();
  const stamp = Date.now();
  try {
    await signIn(organiserPage, organiser);
    const upgraded = await organiserPage.request.post(
      "/api/proxy/api/v1/users/me/organiser",
    );
    expect(upgraded.ok(), await upgraded.text()).toBe(true);
    // The organiser group only appears in tokens issued after the upgrade.
    await organiserPage.request.post("/api/auth/refresh");

    const purchased = await publishEvent(
      organiserPage,
      `E2E Purchase ${stamp}`,
      16,
      testPng,
    );
    const cancelled = await publishEvent(
      organiserPage,
      `E2E Cancel ${stamp}`,
      16,
      testPng,
    );

    // Reservation sets up stock from the EventPublished message.
    await expect
      .poll(
        async () =>
          (await availability(organiserPage, purchased.event_id))?.categories[0]
            ?.free,
        { timeout: 120_000 },
      )
      .toBe(20);

    await signIn(buyerPage, buyer);
    const myTickets = async (eventId: string) => {
      const response = await buyerPage.request.get(
        `/api/proxy/api/v1/tickets?event_id=${eventId}`,
      );
      return ((await response.json()).items ?? []) as Ticket[];
    };

    // Scenario 2: queue, hold, pay, ticket, check-in.
    await buyOneTicket(buyerPage, purchased);
    await expect
      .poll(async () => (await myTickets(purchased.event_id))[0]?.status, {
        timeout: 120_000,
        message: "Ticket Service should issue a ticket for the confirmed order",
      })
      .toBe("VALID");
    const [ticket] = await myTickets(purchased.event_id);
    expect(ticket.qr_payload).toBeTruthy();
    // The sold ticket is taken out of the stock.
    const afterSale = await availability(buyerPage, purchased.event_id);
    expect(afterSale?.categories[0]).toMatchObject({ free: 19, sold: 1 });
    const qr = await buyerPage.request.get(
      `/api/proxy/api/v1/tickets/${ticket.ticket_id}/qr`,
    );
    expect(qr.status()).toBe(200);
    expect(qr.headers()["content-type"]).toContain("image/png");

    const checkIn = () =>
      organiserPage.request.post("/api/proxy/api/v1/tickets/check-in", {
        data: { qr_payload: ticket.qr_payload, event_id: purchased.event_id },
      });
    expect((await checkIn()).status()).toBe(200);
    const again = await checkIn();
    expect(again.status()).toBe(409);
    expect((await again.json()).error.code).toBe("TICKET_ALREADY_CHECKED_IN");

    // Scenario 3: buy a ticket to the second event, then cancel that event.
    // This purchase starts with a declined card, to check that the buyer can
    // retry on the same order and still get the ticket.
    await buyOneTicket(buyerPage, cancelled, { declinedFirst: true });
    await expect
      .poll(async () => (await myTickets(cancelled.event_id))[0]?.status, {
        timeout: 120_000,
      })
      .toBe("VALID");

    const cancel = await organiserPage.request.post(
      `/api/proxy/api/v1/organizer/events/${cancelled.event_id}/cancel`,
      { data: { reason: "End-to-end test cancellation." } },
    );
    expect(cancel.status(), await cancel.text()).toBe(200);

    await expect
      .poll(
        async () => (await availability(buyerPage, cancelled.event_id))?.status,
        {
          timeout: 180_000,
          message: "Reservation should stop selling the cancelled event",
        },
      )
      .toBe("CANCELLED");
    await expect
      .poll(async () => (await myTickets(cancelled.event_id))[0]?.status, {
        timeout: 180_000,
        message:
          "Ticket Service should invalidate the cancelled event's ticket",
      })
      .toBe("INVALIDATED");
    await expect
      .poll(
        async () => {
          const response = await buyerPage.request.get(
            "/api/proxy/api/v1/orders?page_size=50",
          );
          const orders = ((await response.json()).items ?? []) as Order[];
          return orders.find((o) => o.event_id === cancelled.event_id)?.refunds
            .length;
        },
        {
          timeout: 180_000,
          message: "Checkout should record a refund for the cancelled event",
        },
      )
      .toBeGreaterThan(0);
  } finally {
    // Leave no sellable event behind, and no test accounts.
    await cleanUp(
      () => cancelEventsTitled(organiserPage, `E2E Purchase ${stamp}`),
      () => cancelEventsTitled(organiserPage, `E2E Cancel ${stamp}`),
      () => organiserContext.close(),
      () => deleteTestUser(buyer),
      () => deleteTestUser(organiser),
    );
  }
});
