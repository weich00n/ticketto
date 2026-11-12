import { api, type Page } from "@/lib/api/client";
import type { DigitalTicket } from "@/types/ticketing";

// Ticket Service buyer endpoints (/api/v1/tickets).

export type ApiTicket = {
  ticket_id: string;
  order_id: string;
  event_id: string;
  category_id: string;
  ticket_number: number;
  tickets_in_order: number;
  status: "VALID" | "CHECKED_IN" | "INVALIDATED";
  event: { title: string; venue: string; starts_at: string } | null;
  category_name: string | null;
  qr_payload: string | null;
  issued_at: string;
  checked_in_at: string | null;
  invalidated_at: string | null;
  invalidation_reason: string | null;
};

const statuses: Record<ApiTicket["status"], DigitalTicket["status"]> = {
  VALID: "Valid",
  CHECKED_IN: "Past",
  INVALIDATED: "Cancelled",
};

const ticketPath = (ticketId: string) =>
  `/api/v1/tickets/${encodeURIComponent(ticketId)}`;

// Tickets are not assigned seats, so the seat shows the ticket's number in its order.
export const toTicket = (ticket: ApiTicket, buyer: string): DigitalTicket => ({
  id: ticket.ticket_id,
  eventId: ticket.event_id,
  buyer,
  category: ticket.category_name ?? "General admission",
  seat: `${ticket.ticket_number} of ${ticket.tickets_in_order}`,
  status: statuses[ticket.status],
  qrImage:
    ticket.status === "INVALIDATED"
      ? undefined
      : `/api/proxy${ticketPath(ticket.ticket_id)}/qr`,
});

export const listTickets = async () =>
  (await api<Page<ApiTicket>>("/api/v1/tickets?page_size=100")).items;

export const getTicket = (ticketId: string) =>
  api<ApiTicket>(ticketPath(ticketId));
