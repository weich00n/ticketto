/* eslint-disable react-hooks/set-state-in-effect -- Hydrate browser storage after server rendering. */
"use client";
import type {
  DigitalTicket,
  Event,
  Order,
  Reservation,
} from "@/types/ticketing";
import { useEffect, useState } from "react";
import { initialEvents } from "./events";

type Store = {
  events: Event[];
  reservation: Reservation | null;
  orders: Order[];
  tickets: DigitalTicket[];
  admitted: string[];
  saved: string[];
};
const initial: Store = {
  events: initialEvents,
  reservation: null,
  orders: [],
  tickets: [],
  admitted: [],
  saved: [],
};
export function useDemoStore() {
  const [store, setStore] = useState<Store>(initial);
  const [ready, setReady] = useState(false);
  const [toast, setToast] = useState("");
  useEffect(() => {
    try {
      const saved = localStorage.getItem("ticketto-demo-v1");
      if (saved) {
        const parsed = JSON.parse(saved) as Store;
        if (
          !Array.isArray(parsed.events) ||
          !Array.isArray(parsed.orders) ||
          !Array.isArray(parsed.tickets)
        )
          throw new Error("Invalid demo data");
        setStore({ ...initial, ...parsed });
      }
    } catch {
      setToast(
        "Your saved demo could not be loaded. A fresh session is ready.",
      );
    }
    setReady(true);
  }, []);
  useEffect(() => {
    if (ready) {
      try {
        localStorage.setItem("ticketto-demo-v1", JSON.stringify(store));
      } catch {
        setToast(
          "Browser storage is unavailable. Changes will last for this session.",
        );
      }
    }
  }, [store, ready]);
  const update = (patch: Partial<Store>) =>
    setStore((current) => ({ ...current, ...patch }));
  return { store, update, toast, setToast };
}
