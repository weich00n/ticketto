import type { Reservation } from "@/types/ticketing";

// Carries a booking between screens in this tab: the admission token from the
// Queue Service to ticket selection, and the reservation shown on checkout and
// the confirmation page. The services stay the source of truth; this only
// remembers what the screens need to ask them for.

export type Booking = Reservation & {
  holdId: string;
  buyer?: string;
  orderId?: string;
};

type AdmissionTicket = { token: string; selectionExpires: number };

const admissionKey = (eventId: string) => `ticketto-admission-${eventId}`;
const bookingKey = "ticketto-booking";

function read<T>(key: string): T | null {
  try {
    const value = sessionStorage.getItem(key);
    return value ? (JSON.parse(value) as T) : null;
  } catch {
    return null;
  }
}

function write(key: string, value: unknown) {
  try {
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Storage can be unavailable (private mode); the booking then lasts until reload.
  }
}

export const saveAdmission = (eventId: string, admission: AdmissionTicket) =>
  write(admissionKey(eventId), admission);

// Holds can be created only before the admission's selection deadline.
export function readAdmission(eventId: string) {
  const admission = read<AdmissionTicket>(admissionKey(eventId));
  if (!admission || !(admission.selectionExpires > Date.now())) return null;
  return admission;
}

export const saveBooking = (booking: Booking | null) =>
  write(bookingKey, booking);

export const readBooking = () => read<Booking>(bookingKey);
