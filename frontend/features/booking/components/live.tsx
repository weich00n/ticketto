/* eslint-disable react-hooks/set-state-in-effect -- Read the tab's booking from sessionStorage after hydration. */
"use client";
// Booking screens backed by the Queue, Reservation, Checkout and Ticket
// services. They reuse the demo screens' presentation.

import { singaporeDateTime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Empty, Timer } from "@/components/ui/shared";
import { getEventsById, placeholderEvent } from "@/features/events/api";
import { ApiError } from "@/lib/api/client";
import { useRemote } from "@/lib/api/use-remote";
import type { SessionUser } from "@/lib/auth/tokens";
import type { Event, Reservation } from "@/types/ticketing";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  readAdmission,
  readBooking,
  saveAdmission,
  saveBooking,
  type Booking,
} from "../booking-session";
import {
  createOrder,
  getOrder,
  type CheckoutOrder,
  type OrderStatus,
} from "../checkout-api";
import { isTransient, maxPollFailures } from "../polling";
import { joinQueue, queueStatus, type QueueEntry } from "../queue-api";
import {
  createHold,
  eventAvailability,
  withAvailability,
  getHold,
  hasPassed,
  toLocalClock,
  type Hold,
} from "../reservations-api";
import { getTicket, listTickets, toTicket } from "../tickets-api";
import { Checkout } from "./checkout";
import { Payment } from "./payment";
import { QueueView } from "./queue";
import { Selection } from "./selection";
import { Success } from "./success";
import { TicketView } from "./ticket-view";
import { Tickets } from "./tickets";

const Loading = ({ title }: { title: string }) => (
  <Empty title={title} text="Opening the doors…" action={<span />} />
);

const defaultPollSeconds = 5;

const timeLabel = singaporeDateTime;

function queueClosedMessage(error: unknown) {
  if (!(error instanceof ApiError)) return "The waiting room is unavailable.";
  if (error.code === "QUEUE_NOT_OPEN")
    return "The waiting room is not open yet. Come back shortly before tickets are released.";
  return error.message;
}

export function LiveQueue({ event }: { event: Event }) {
  const [entry, setEntry] = useState<QueueEntry | null>(null);
  const [error, setError] = useState("");
  // Set while the buyer's last turn is over but its place is not yet released.
  const [rejoinAt, setRejoinAt] = useState<number>();

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    // One dropped request should not end the buyer's place in line, so a failed
    // call is tried again until it fails maxPollFailures times in a row.
    function run(call: () => Promise<QueueEntry>) {
      call()
        .then((next) => {
          failures = 0;
          track(next);
        })
        .catch((e) => {
          if (!active) return;
          failures += 1;
          if (!isTransient(e) || failures >= maxPollFailures)
            setError(queueClosedMessage(e));
          else timer = setTimeout(() => run(call), defaultPollSeconds * 1000);
        });
    }
    function track(next: QueueEntry) {
      if (!active) return;
      setEntry(next);
      setRejoinAt(undefined);
      if (next.entry_status === "ADMITTED" && next.admission_token) {
        const selectionExpires = Date.parse(
          next.admission?.selection_expires_at ?? "",
        );
        if (selectionExpires > Date.now()) {
          saveAdmission(event.id, {
            token: next.admission_token,
            selectionExpires,
          });
          return;
        }
        // The buyer's time to choose is over (for example, a hold ran out), but
        // the Queue Service keeps the place until the admission ends. Joining
        // gives the old admission back until then, so wait and join again.
        const admissionEnds = Date.parse(
          next.admission?.admission_expires_at ?? "",
        );
        const wait = Math.max(admissionEnds - Date.now(), 0) || 5_000;
        setRejoinAt(Date.now() + wait);
        timer = setTimeout(() => run(() => joinQueue(event.id)), wait + 1_000);
        return;
      }
      if (next.entry_status === "NONE") {
        setError("The waiting room for this event has closed.");
        return;
      }
      const seconds = next.poll_after_seconds ?? defaultPollSeconds;
      timer = setTimeout(
        () => run(() => queueStatus(event.id)),
        seconds * 1000,
      );
    }
    run(() => joinQueue(event.id));
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [event.id]);

  if (error)
    return <Empty title="The waiting room is unavailable" text={error} />;
  if (!entry) return <Loading title="Joining the waiting room…" />;
  if (rejoinAt)
    return (
      <Empty
        title="Your last turn has ended"
        text={`Your tickets were not held in time, and your place is still being released. You will rejoin the waiting room automatically at ${timeLabel(new Date(rejoinAt).toISOString())}. Keep this page open.`}
      />
    );
  const admitted = entry.entry_status === "ADMITTED";
  const position = entry.position ?? null;
  return (
    <QueueView
      event={event}
      admitted={admitted}
      position={position}
      waitText={
        position === null && entry.release_at
          ? `Places are drawn when tickets are released at ${timeLabel(entry.release_at)}.`
          : "You will move forward automatically. Keep this page open."
      }
    />
  );
}

export function LiveSelection({ event }: { event: Event }) {
  const router = useRouter();
  const [admission, setAdmission] =
    useState<ReturnType<typeof readAdmission>>(null);
  useEffect(() => setAdmission(readAdmission(event.id)), [event.id]);
  // Without live figures the event's totals still let the buyer choose; the
  // Reservation Service rejects a hold it cannot fill.
  const { state } = useRemote(`availability-${event.id}`, () =>
    eventAvailability(event.id),
  );
  if (state.status === "loading")
    return <Loading title="Checking availability…" />;
  const live =
    state.status === "ready" ? withAvailability(event, state.data) : event;

  async function reserve(r: Reservation) {
    if (
      !admission ||
      !r.categoryId ||
      Date.now() >= admission.selectionExpires
    ) {
      router.push(`/queue/${event.id}`);
      return;
    }
    const hold = await createHold({
      eventId: event.id,
      categoryId: r.categoryId,
      quantity: r.quantity,
      admissionToken: admission.token,
    });
    saveBooking({
      ...r,
      holdId: hold.hold_id,
      quantity: hold.quantity,
      price: hold.unit_amount / 100,
      seats: [],
      expires: toLocalClock(hold.payment_expires_at, hold.server_time),
    });
    router.push("/checkout");
  }

  return (
    <Selection
      event={live}
      admitted={!!admission}
      selectionExpires={admission?.selectionExpires}
      reserve={reserve}
      bookingFee={0}
    />
  );
}

function useBooking() {
  const [booking, setBooking] = useState<Booking | null | undefined>();
  useEffect(() => setBooking(readBooking()), []);
  return [booking, setBooking] as const;
}

const holdPollMs = 3000;

// Payment is due five minutes after the hold is made, but the hold lasts a minute
// longer so a payment that is still being confirmed can land. Once the hold or
// the order settles, the order page says whether the buyer paid; this screen
// never decides that itself, so a late payment is not shown as missing.
function AwaitingPayment({ hold, booking }: { hold: Hold; booking: Booking }) {
  const router = useRouter();
  const [holdEnds] = useState(() =>
    toLocalClock(hold.hold_expires_at, hold.server_time),
  );

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const check = async () => {
      try {
        const [latest, order] = await Promise.all([
          getHold(booking.holdId),
          booking.orderId ? getOrder(booking.orderId) : undefined,
        ]);
        if (!active) return;
        if (
          latest.status !== "ACTIVE" ||
          (order && order.status !== "PENDING_PAYMENT")
        ) {
          router.replace("/checkout/success");
          return;
        }
      } catch {
        // A failed check is tried again on the next round.
      }
      if (active) timer = setTimeout(check, holdPollMs);
    };
    check();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [booking.holdId, booking.orderId, router]);

  return (
    <Empty
      title="Waiting for payment confirmation"
      text="Your payment time is over. If you have already paid, your tickets will appear in a moment. We are holding them a little longer in case the payment is still being confirmed."
      action={<Timer expires={holdEnds} />}
    />
  );
}

export function LiveCheckout({
  events,
  stripeKey,
}: {
  events: Event[];
  stripeKey: string;
}) {
  const router = useRouter();
  const [booking, setBooking] = useBooking();
  const [payment, setPayment] = useState<{
    clientSecret: string;
    total: number;
  } | null>(null);
  const [expired, setExpired] = useState(false);
  const { state } = useRemote(`hold-${booking?.holdId}`, async () => {
    if (!booking) return null;
    const hold = await getHold(booking.holdId);
    // Fixed when the hold arrives. Worked out again on each render, the deadline
    // would move later as the buyer fills in the form, and the timer would reset.
    return {
      hold,
      paymentEnds: toLocalClock(hold.payment_expires_at, hold.server_time),
    };
  });
  const hold = state.status === "ready" ? (state.data?.hold ?? null) : null;
  const paid = hold?.status === "CONFIRMED";
  // Once an order exists and its hold has ended, the order says whether the
  // buyer paid, so a payment confirmed late is not shown as an expired hold.
  const settled =
    paid || (!!booking?.orderId && !!hold && hold.status !== "ACTIVE");
  useEffect(() => {
    if (settled) router.replace("/checkout/success");
  }, [settled, router]);
  if (booking === undefined || state.status === "loading")
    return <Loading title="Loading your reservation…" />;
  if (settled)
    return (
      <Loading
        title={
          paid ? "Payment received. Opening your order…" : "Opening your order…"
        }
      />
    );
  // An ended hold shows as expired, so the buyer can select tickets again.
  const reservation =
    booking && state.status === "ready" && state.data
      ? {
          ...booking,
          expires:
            state.data.hold.status === "ACTIVE" ? state.data.paymentEnds : 0,
        }
      : booking && { ...booking, expires: 0 };
  const paymentOver =
    expired || (!!hold && hasPassed(hold.payment_expires_at, hold.server_time));
  // Only a buyer who started paying can have a payment on its way.
  if (booking?.orderId && hold?.status === "ACTIVE" && paymentOver)
    return <AwaitingPayment hold={hold} booking={booking} />;
  if (payment && reservation && !expired)
    return (
      <Payment
        stripeKey={stripeKey}
        clientSecret={payment.clientSecret}
        total={payment.total}
        expires={reservation.expires}
        onExpired={() => setExpired(true)}
        onPaid={() => router.push("/checkout/success")}
      />
    );
  return (
    <Checkout
      reservation={
        expired && reservation ? { ...reservation, expires: 0 } : reservation
      }
      events={events}
      simulated={false}
      pay={async ({ name, email }) => {
        if (!booking) return;
        const order = await createOrder(booking.holdId, email);
        const next = { ...booking, buyer: name, orderId: order.order_id };
        saveBooking(next);
        // Kept on this screen too: if the payment timer runs out mid-payment,
        // the buyer waits for confirmation instead of seeing an expired hold.
        setBooking(next);
        if (order.status !== "PENDING_PAYMENT") {
          router.push("/checkout/success");
          return;
        }
        if (!order.client_secret)
          throw new Error(
            "The payment provider is unavailable. Please try again in a moment.",
          );
        setPayment({
          clientSecret: order.client_secret,
          total: order.total_amount / 100,
        });
      }}
    />
  );
}

const orderPollMs = 3000;
// The order can still change on its own, so the page keeps asking.
const pendingStatuses: OrderStatus[] = ["PENDING_PAYMENT", "CONFIRMING"];

export function LiveSuccess({ events }: { events: Event[] }) {
  const [booking] = useBooking();
  const [order, setOrder] = useState<CheckoutOrder | null>(null);
  const [error, setError] = useState("");
  const orderId = booking?.orderId;

  useEffect(() => {
    if (orderId === undefined) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = () =>
      getOrder(orderId)
        .then((next) => {
          if (!active) return;
          setOrder(next);
          if (pendingStatuses.includes(next.status))
            timer = setTimeout(poll, orderPollMs);
        })
        .catch((e) => {
          if (active)
            setError(
              e instanceof ApiError
                ? e.message
                : "We could not check your order.",
            );
        });
    poll();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [orderId]);

  if (booking === undefined) return <Loading title="Loading your order…" />;
  if (!booking || orderId === undefined)
    return <Empty title="No completed booking yet" />;
  if (error)
    return <Empty title="Your order status is unavailable" text={error} />;
  if (!order) return <Loading title="Confirming your order…" />;
  if (order.status === "CONFIRMING")
    return <Loading title="Confirming your order…" />;
  // Stripe tells Checkout a few seconds after the buyer pays, so a paid order
  // can still be waiting here.
  if (order.status === "PENDING_PAYMENT")
    return (
      <Empty
        title="Confirming your payment"
        text="This usually takes a few seconds. If you did not finish paying, you can return to payment."
        action={
          <Button asChild>
            <Link href="/checkout">Return to payment</Link>
          </Button>
        }
      />
    );
  if (order.status === "RECONCILIATION")
    return (
      <Empty
        title="We are checking your payment"
        text="Your payment was received but your booking needs a manual check. We will email you once it is resolved."
      />
    );
  if (order.status !== "CONFIRMED")
    return (
      <Empty
        title={
          order.status === "EXPIRED"
            ? "Your payment was not completed"
            : "This order has been cancelled"
        }
        text={
          order.refunds.length > 0
            ? "Your payment is being refunded to your original payment method."
            : order.status === "EXPIRED"
              ? "The payment window closed before payment was confirmed. Your tickets have been released."
              : "No payment has been taken."
        }
        action={
          order.status === "EXPIRED" ? (
            <Button asChild>
              <Link href={`/queue/${booking.eventId}`}>
                Join the waiting room again
              </Link>
            </Button>
          ) : (
            <span />
          )
        }
      />
    );
  return (
    <Success
      simulated={false}
      events={events}
      order={{
        id: order.order_id,
        reservation: booking,
        buyer: booking.buyer ?? "",
        email: order.delivery_email ?? "",
        total: order.total_amount / 100,
        date: order.created_at,
      }}
    />
  );
}

export function LiveTickets({ user }: { user: SessionUser | null }) {
  // The tickets carry only the event's name, venue and start, so the events
  // themselves are loaded for their pictures and dates.
  const { state } = useRemote("tickets", async () => {
    const tickets = await listTickets();
    const events = await getEventsById(tickets.map((t) => t.event_id));
    return { tickets, events };
  });
  if (state.status === "loading")
    return <Loading title="Loading your tickets…" />;
  if (state.status === "error")
    return <Empty title="Your tickets are unavailable" text={state.message} />;
  const { tickets, events } = state.data;
  return (
    <Tickets
      tickets={tickets.map((t) => toTicket(t, user?.name ?? ""))}
      events={tickets.map(
        (t) =>
          events.find((e) => e.id === t.event_id) ??
          placeholderEvent(t.event_id, t.event),
      )}
    />
  );
}

export function LiveTicketView({
  ticketId,
  user,
}: {
  ticketId: string;
  user: SessionUser | null;
}) {
  const { state } = useRemote(`ticket-${ticketId}`, () => getTicket(ticketId));
  if (state.status === "loading")
    return <Loading title="Loading your ticket…" />;
  if (state.status === "error")
    return state.error instanceof ApiError && state.error.status === 404 ? (
      <TicketView ticket={undefined} events={[]} />
    ) : (
      <Empty title="Your ticket is unavailable" text={state.message} />
    );
  return (
    <TicketView
      ticket={toTicket(state.data, user?.name ?? "")}
      events={[placeholderEvent(state.data.event_id, state.data.event)]}
    />
  );
}
