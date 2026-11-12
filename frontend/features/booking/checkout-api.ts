import { api } from "@/lib/api/client";

// Checkout Service (/api/v1/orders). Amounts are in cents.

export type OrderStatus =
  | "PENDING_PAYMENT"
  | "CONFIRMING"
  | "CONFIRMED"
  | "EXPIRED"
  | "CANCELLED"
  | "RECONCILIATION";

export type CheckoutOrder = {
  order_id: string;
  status: OrderStatus;
  hold_id: string;
  event_id: string;
  quantity: number;
  unit_amount: number;
  total_amount: number;
  currency: string;
  delivery_email: string | null;
  payment: {
    status: "PENDING" | "SUCCEEDED" | "FAILED" | "CANCELLED";
    amount: number;
    currency: string;
  } | null;
  refunds: { refund_id: string; amount: number; status: string }[];
  // Stripe client secret for the Payment Element; set while the order is unpaid.
  client_secret: string | null;
  created_at: string;
  updated_at: string;
};

// One order per hold, so a retried request returns the same order and its
// client secret again.
export const createOrder = (holdId: string, email: string) =>
  api<CheckoutOrder>("/api/v1/orders", {
    method: "POST",
    headers: { "Idempotency-Key": `hold-${holdId}` },
    json: { hold_id: holdId, delivery_email: email },
  });

export const getOrder = (orderId: string) =>
  api<CheckoutOrder>(`/api/v1/orders/${encodeURIComponent(orderId)}`);
