/* eslint-disable react-hooks/set-state-in-effect -- Synchronize browser-only storage and navigation state after hydration. */
"use client";
import { liveScreen } from "@/components/layout/live-screens";
import { NotificationBell } from "@/components/layout/notification-bell";
import { ProfileMenu } from "@/components/layout/profile-menu";
import { Empty, Footer } from "@/components/ui/shared";
import { Checkout, demoOrder } from "@/features/booking/components/checkout";
import { Queue } from "@/features/booking/components/queue";
import { Selection } from "@/features/booking/components/selection";
import { Success } from "@/features/booking/components/success";
import { TicketView } from "@/features/booking/components/ticket-view";
import { Tickets } from "@/features/booking/components/tickets";
import { Browse } from "@/features/events/components/browse";
import { EventDetail } from "@/features/events/components/event-detail";
import { Home } from "@/features/events/components/home";
import { useEvents } from "@/features/events/use-events";
import { EventForm } from "@/features/organizer/components/event-form";
import { BecomeOrganiser } from "@/features/organizer/components/become-organiser";
import { Organizer } from "@/features/organizer/components/organizer";
import { OrganizerDetail } from "@/features/organizer/components/organizer-detail";
import type { SessionUser } from "@/lib/auth/tokens";
import { useDemoStore } from "@/lib/demo/use-demo-store";
import { ArrowUpRight, CheckCircle2, Menu, UserRound, X } from "lucide-react";
import Image from "next/image";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

export function Platform({
  user,
  demoData,
  stripeKey,
}: {
  user: SessionUser | null;
  demoData: boolean;
  stripeKey: string;
}) {
  const path = usePathname();
  const router = useRouter();
  const { store, update, toast, setToast } = useDemoStore();
  const publicEventId =
    path.startsWith("/events/") || path.startsWith("/queue/")
      ? path.split("/")[2]
      : undefined;
  const eventsState = useEvents(demoData, store.events, path, publicEventId);
  const events = eventsState.status === "ready" ? eventsState.events : [];
  const [mobile, setMobile] = useState(false);
  useEffect(() => {
    if (toast) {
      const t = setTimeout(() => setToast(""), 4500);
      return () => clearTimeout(t);
    }
  }, [toast, setToast]);
  useEffect(() => setMobile(false), [path]);
  const notify = (message: string) => setToast(message);
  const save = (id: string) => {
    const exists = store.saved.includes(id);
    update({
      saved: exists
        ? store.saved.filter((x) => x !== id)
        : [...store.saved, id],
    });
    notify(
      exists
        ? "Event removed from your saved list."
        : "Event saved. Find it with the Saved filter in Browse Events.",
    );
  };
  const event = events.find((e) => e.id === publicEventId);
  const eventName =
    event && path.startsWith("/events/") && !path.endsWith("/tickets")
      ? event.name
      : undefined;
  useEffect(() => {
    if (eventName) document.title = `${eventName} — Ticketto`;
  }, [eventName]);
  // Checkout shows the event being booked, so it waits for the event list too.
  const needsEvents =
    path === "/" ||
    path === "/events" ||
    publicEventId !== undefined ||
    path.startsWith("/checkout");
  const live = demoData
    ? undefined
    : liveScreen({ path, event, events, user, notify, stripeKey });
  let content: ReactNode;
  if (path.startsWith("/organizer") && !user?.organiser)
    content = <BecomeOrganiser />;
  else if (needsEvents && eventsState.status === "loading")
    content = <Empty title="Loading events…" text="Opening the doors…" />;
  else if (needsEvents && eventsState.status === "error")
    content = (
      <Empty title="Events are unavailable" text={eventsState.message} />
    );
  else if (live) content = live;
  else if (path === "/")
    content = <Home events={events} saved={store.saved} save={save} />;
  else if (path === "/events")
    content = <Browse events={events} saved={store.saved} save={save} />;
  else if (path.startsWith("/queue/"))
    content = event ? (
      <Queue
        event={event}
        admitted={store.admitted.includes(event.id)}
        admit={() =>
          update({ admitted: [...new Set([...store.admitted, event.id])] })
        }
      />
    ) : (
      <Empty title="Event not found" />
    );
  else if (path.startsWith("/events/") && path.endsWith("/tickets"))
    content = event ? (
      <Selection
        event={event}
        reserve={(r) => {
          update({ reservation: r });
          router.push("/checkout");
        }}
        admitted={store.admitted.includes(event.id)}
      />
    ) : (
      <Empty title="Event not found" />
    );
  else if (path.startsWith("/events/"))
    content = event ? (
      <EventDetail event={event} demo />
    ) : (
      <Empty title="Event not found" />
    );
  else if (path === "/checkout/success")
    content = <Success order={store.orders.at(-1)} events={store.events} />;
  else if (path === "/checkout")
    content = (
      <Checkout
        reservation={store.reservation}
        events={store.events}
        pay={async (details) => {
          await new Promise((resolve) => setTimeout(resolve, 1500));
          if (details.decline)
            throw new Error(
              "The simulated payment was declined. Turn off the demo failure option and try again.",
            );
          const r = store.reservation;
          const paidEvent = store.events.find((e) => e.id === r?.eventId);
          if (!r || !paidEvent) return;
          const { order, tickets } = demoOrder(r, paidEvent, details);
          update({
            orders: [...store.orders, order],
            tickets: [...store.tickets, ...tickets],
            events: store.events.map((e) =>
              e.id === order.reservation.eventId
                ? {
                    ...e,
                    categories: e.categories.map((c) =>
                      c.name === order.reservation.category
                        ? {
                            ...c,
                            available: Math.max(
                              0,
                              c.available - order.reservation.quantity,
                            ),
                          }
                        : c,
                    ),
                  }
                : e,
            ),
            reservation: null,
          });
          router.push("/checkout/success");
        }}
      />
    );
  else if (path === "/tickets")
    content = <Tickets tickets={store.tickets} events={store.events} />;
  else if (path.startsWith("/tickets/"))
    content = (
      <TicketView
        ticket={store.tickets.find((t) => t.id === path.split("/")[2])}
        events={store.events}
      />
    );
  else if (
    path === "/organizer/events/new" ||
    (path.startsWith("/organizer/events/") && path.endsWith("/edit"))
  )
    content = (
      <EventForm
        key={path}
        event={store.events.find((e) => e.id === path.split("/")[3])}
        save={(e) => {
          update({
            events: store.events.some((x) => x.id === e.id)
              ? store.events.map((x) => (x.id === e.id ? e : x))
              : [...store.events, e],
          });
          notify(
            e.status === "Draft"
              ? "Draft saved."
              : "Event published. Your doors are open.",
          );
          router.push("/organizer");
        }}
      />
    );
  else if (path.startsWith("/organizer/events/"))
    content = (
      <OrganizerDetail
        event={store.events.find((e) => e.id === path.split("/")[3])}
        orders={store.orders}
        cancel={(id) => {
          update({
            events: store.events.map((e) =>
              e.id === id ? { ...e, status: "Event cancelled" } : e,
            ),
            tickets: store.tickets.map((t) =>
              t.eventId === id ? { ...t, status: "Cancelled" } : t,
            ),
          });
          notify("Event cancelled. Mock buyer notifications queued.");
        }}
      />
    );
  else if (path === "/organizer")
    content = <Organizer events={store.events} orders={store.orders} />;
  else content = <Empty title="This page has wandered off." />;
  return (
    <>
      <header className="site-header">
        <div className="nav-wrap">
          <Link href="/" className="brand">
            <Image
              className="brand-logo brand-logo-square"
              src="/chris.jpg"
              alt="Ticketto"
              width={400}
              height={400}
              priority
            />
          </Link>
          <nav
            className={mobile ? "nav-links open" : "nav-links"}
            aria-label="Main navigation"
          >
            <Link className={path === "/events" ? "active" : ""} href="/events">
              Browse Events
            </Link>
            <Link
              className={path.startsWith("/tickets") ? "active" : ""}
              href="/tickets"
            >
              My Tickets
            </Link>
            <Link
              className={path.startsWith("/organizer") ? "active" : ""}
              href="/organizer"
            >
              For Organizers <ArrowUpRight size={13} />
            </Link>
          </nav>
          <div className="nav-actions">
            {user && <NotificationBell live={!demoData} />}
            {user ? (
              <ProfileMenu user={user} />
            ) : (
              <Link className="profile" href="/login">
                <span className="avatar">
                  <UserRound size={16} />
                </span>
                <span>Sign in</span>
              </Link>
            )}
            <button
              className="mobile-toggle"
              onClick={() => setMobile(!mobile)}
              aria-label="Toggle navigation"
              aria-expanded={mobile}
            >
              {mobile ? <X /> : <Menu />}
            </button>
          </div>
        </div>
      </header>
      <main>{content}</main>
      <Footer demo={demoData} />
      {toast && (
        <div className="toast" role="status">
          <CheckCircle2 size={18} />
          {toast}
          <button
            aria-label="Dismiss notification"
            onClick={() => setToast("")}
          >
            <X size={16} />
          </button>
        </div>
      )}
    </>
  );
}
