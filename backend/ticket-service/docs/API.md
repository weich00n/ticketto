# Ticket Service API Endpoints

Ticket Service issues one signed QR ticket per purchased admission, lets buyers view their tickets, tells Notification Service to email them (`TicketIssued`), lets venue staff check each valid ticket in once, and invalidates tickets when an event is cancelled or an order is refunded.

It does not manage events, live availability, holds, orders, payments or refunds, and it sends no email itself. It learns about confirmed orders, cancellations and refunds only through RabbitMQ messages, reports issued tickets only by publishing `TicketIssued`, and never reads another service's database.

## Conventions

| Item | Rule |
| --- | --- |
| Base path | Public routes: `/api/v1`. Internal routes: `/internal/v1` |
| Format | JSON over HTTP; HTTPS for public access. `GET /api/v1/tickets/{ticket_id}/qr` returns `image/png` |
| Field names | snake_case |
| Identifiers | UUID strings |
| Time | UTC, ISO 8601, whole seconds, e.g. `2026-11-20T12:00:00Z` |
| Pagination | `page` (default 1) and `page_size` (default 20, max 100). Lists return `items`, `page`, `page_size` and `total_items` |
| Correlation | Clients may send `X-Correlation-Id`. The service returns it in the response header and in error bodies, and logs it. If absent, the service generates one. For messages, the envelope's `correlation_id` (or the AMQP `correlation_id` property, else `message_id`) is used in logs |

### Authentication

There is no authentication yet; users and authentication will be designed later. Until then, buyer routes identify the buyer with an identity header, the interim approach Queue Service uses:

| Header | Meaning |
| --- | --- |
| `X-User-Id` | The buyer's `user_id`; must equal `user_id` in `OrderConfirmed`. Required on buyer routes. Optional on check-in, where it is recorded as the scanner |

Buyers can see only their own tickets. Requests for another buyer's ticket return `404`, not `403`, so the response does not reveal that the ticket exists. Check-in is open to any caller.

When authentication is added, setting `AUTH_MODE=gateway` makes the service read the identity API Gateway forwards after validating the Cognito token (`X-User-Sub` as the `user_id`, `X-User-Username`, and `X-User-Groups` such as `[organiser]`), ignore `X-User-Id`, and require one of the groups in `CHECK_IN_GROUPS` for check-in (default `organiser` or `staff`; the team's Cognito roles are `buyer` and `organiser`). Nothing else in this contract changes.

Every error uses `{"error": {"code": "<CODE>", "message": "<text>", "details": {}}, "correlation_id": "<uuid>"}`; `details` is present only where a row below says so.

| HTTP | Code | When |
| --- | --- | --- |
| 400 | `VALIDATION_ERROR` | Missing, malformed or invalid fields or query parameters |
| 400 | `INVALID_QR_CODE` | Check-in QR payload is malformed or its signature is not genuine |
| 401 | `UNAUTHENTICATED` | `X-User-Id` missing on a buyer route (`X-User-Sub` once `AUTH_MODE=gateway`) |
| 403 | `FORBIDDEN` | Only with `AUTH_MODE=gateway`: check-in caller is not an organiser or staff (`CHECK_IN_GROUPS`) |
| 404 | `TICKET_NOT_FOUND` | Ticket does not exist or belongs to another buyer |
| 404 | `ORDER_NOT_FOUND` | No `OrderConfirmed` has been processed for this order |
| 409 | `TICKET_ALREADY_CHECKED_IN` | Ticket was already checked in; `details.checked_in_at` |
| 409 | `TICKET_INVALIDATED` | Ticket was invalidated; `details.invalidation_reason` |
| 409 | `TICKET_EVENT_MISMATCH` | Ticket is for a different event than the one being scanned; `details.ticket_event_id` |
| 409 | `TICKET_ISSUED_NOT_REPUBLISHABLE` | The order has no valid tickets to republish (rejected, invalidated, or every ticket used); `details.issuance_status`, `details.valid_tickets` |
| 500 | `INTERNAL_ERROR` | Unexpected error; logged with the correlation ID |
| 503 | `SERVICE_UNAVAILABLE` | Database temporarily unavailable; retry |

## Ticket status

| Status | Meaning |
| --- | --- |
| `VALID` | Issued for a confirmed order; can be checked in once |
| `CHECKED_IN` | Scanned at the venue; final |
| `INVALIDATED` | The event was cancelled (`EVENT_CANCELLED`) or the order was refunded (`ORDER_REFUNDED`); final |

Allowed transitions: `VALID → CHECKED_IN`, `VALID → INVALIDATED`. Both are conditional updates on `status = VALID`, so a check-in racing a cancellation produces exactly one outcome. A ticket already `CHECKED_IN` when its event is cancelled keeps that status as the record of entry.

## Ticket object

### Buyer view

```json
{
  "ticket_id": "6f0d3c2a-8b1e-4f7a-9c5d-2e4b6a8c0d1f",
  "order_id": "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b",
  "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
  "category_id": "b05e8625-6d43-4252-ad2c-bda3222a769b",
  "ticket_number": 1,
  "tickets_in_order": 2,
  "status": "VALID",
  "event": {
    "title": "The Midnight Symphony",
    "venue": "Victoria Theatre & Concert Hall",
    "starts_at": "2026-11-21T11:30:00Z"
  },
  "category_name": "CAT 1",
  "qr_payload": "TKT1.k1.6f0d3c2a-8b1e-4f7a-9c5d-2e4b6a8c0d1f.dGhpcy1pcy1hbi1leGFtcGxlLXNpZ25hdHVyZQ",
  "issued_at": "2026-11-20T12:06:16Z",
  "checked_in_at": null,
  "invalidated_at": null,
  "invalidation_reason": null
}
```

`ticket_number` is the ticket's position within its order (1 to `tickets_in_order`); `(order_id, ticket_number)` is unique, which prevents duplicate issuance. `event` and `category_name` are a snapshot cached from `EventPublished` (or fetched once from Event Service) and may be `null` until known. `qr_payload` is `null` when the ticket is `INVALIDATED`. `invalidation_reason` is `EVENT_CANCELLED`, `ORDER_REFUNDED` or `null`. The ticket email is Notification Service's, so the ticket object no longer has an email status.

### QR payload

The QR code encodes `TKT1.<key_id>.<ticket_id>.<signature>`. `ticket_id` is a random UUID (version 4), so it cannot be guessed. `signature` is base64url (no padding) of HMAC-SHA256 over `TKT1.<key_id>.<ticket_id>`; only Ticket Service holds the secrets, and check-in always happens there. `key_id` allows key rotation: new tickets use the active key, and older keys stay configured so existing tickets still verify. The QR code contains no personal data.

---

## Ticket Service API endpoints

The Ticket Service exposes 9 HTTP endpoints: 3 for buyers, 1 for check-in, 3 internal and 2 health checks. All bodies are JSON (`application/json`) unless a row says otherwise.

## Buyer endpoints

All routes in this section require the `X-User-Id` header.

### 1. List my tickets

| Field | Value |
| --- | --- |
| Endpoint | `GET /api/v1/tickets?event_id=&order_id=&status=&page=1&page_size=20` |
| Parameters | Header: `X-User-Id` (required). Query: `event_id` (UUID, optional), `order_id` (UUID, optional; the order confirmation page can poll this until tickets appear), `status` (optional: `VALID`, `CHECKED_IN`, `INVALIDATED`), `page` (optional, default 1, integer ≥ 1), `page_size` (optional, default 20, integer 1–100). Empty values are ignored |
| Request body | None |
| Response type | application/json |
| 200 OK | Returns the caller's tickets in any status, newest first (`issued_at` descending, then `order_id`, then `ticket_number`); an empty list if there are none. `{"items": [{ "...": "ticket object" }], "page": 1, "page_size": 20, "total_items": 2}` |
| 400 Bad Request | `{"error": {"code": "VALIDATION_ERROR", "message": "…", "details": {"status": "…"}}}` for an invalid filter, `page` or `page_size` |
| 401 Unauthorized | `{"error": {"code": "UNAUTHENTICATED", "message": "The X-User-Id header is required."}}` |

### 2. Get a ticket

| Field | Value |
| --- | --- |
| Endpoint | `GET /api/v1/tickets/{ticket_id}` |
| Parameters | Path: `ticket_id` (UUID, required). Header: `X-User-Id` (required) |
| Request body | None |
| Response type | application/json |
| 200 OK | The ticket object, including `qr_payload` |
| 400 Bad Request | `{"error": {"code": "VALIDATION_ERROR", "message": "…"}}` if `ticket_id` is not a UUID |
| 401 Unauthorized | `{"error": {"code": "UNAUTHENTICATED", "message": "…"}}` |
| 404 Not Found | `{"error": {"code": "TICKET_NOT_FOUND", "message": "…"}}` if the ticket does not exist or belongs to another buyer |

### 3. Get a ticket's QR image

| Field | Value |
| --- | --- |
| Endpoint | `GET /api/v1/tickets/{ticket_id}/qr?scale=8` |
| Parameters | Path: `ticket_id` (UUID, required). Header: `X-User-Id` (required). Query: `scale` (optional, pixels per QR module, default 8, integer 2–20) |
| Request body | None |
| Response type | image/png on success (sent with `Cache-Control: private, no-store`); application/json on error |
| 200 OK | PNG image of the ticket's `qr_payload` |
| 400 Bad Request | `{"error": {"code": "VALIDATION_ERROR", "message": "…"}}` for a malformed `ticket_id` or `scale` |
| 401 Unauthorized | `{"error": {"code": "UNAUTHENTICATED", "message": "…"}}` |
| 404 Not Found | `{"error": {"code": "TICKET_NOT_FOUND", "message": "…"}}` |
| 409 Conflict | `{"error": {"code": "TICKET_INVALIDATED", "message": "…", "details": {"invalidation_reason": "EVENT_CANCELLED"}}}` if the ticket was invalidated |

## Check-in endpoint

No authentication yet: any caller may check a ticket in. An `X-User-Id` header, if sent, is recorded as the scanner. With `AUTH_MODE=gateway`, the caller must be in one of `CHECK_IN_GROUPS` (`organiser` or `staff` by default).

### 4. Check in a ticket

| Field | Value |
| --- | --- |
| Endpoint | `POST /api/v1/tickets/check-in` |
| Parameters | Header: `X-User-Id` (optional, recorded as the scanner) |
| Request body | `{"qr_payload": "TKT1.k1.6f0d3c2a-8b1e-4f7a-9c5d-2e4b6a8c0d1f.dGhpcy1pcy1hbi1leGFtcGxlLXNpZ25hdHVyZQ", "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90"}`. `event_id` is the event the staff member is admitting guests to |
| Response type | application/json |
| 200 OK | Verifies the signature, then atomically changes the ticket from `VALID` to `CHECKED_IN` and records the check-in. `{"ticket_id": "6f0d3c2a-…", "event_id": "c4a1e8f2-…", "category_id": "b05e8625-…", "category_name": "CAT 1", "ticket_number": 1, "tickets_in_order": 2, "status": "CHECKED_IN", "checked_in_at": "2026-11-20T18:15:02Z"}` |
| 400 Bad Request | `{"error": {"code": "VALIDATION_ERROR", "message": "…"}}` if a field is missing or `event_id` is not a UUID; `{"error": {"code": "INVALID_QR_CODE", "message": "…"}}` if the payload is malformed or the signature is not genuine |
| 401 Unauthorized | Only with `AUTH_MODE=gateway`: `{"error": {"code": "UNAUTHENTICATED", "message": "…"}}` |
| 403 Forbidden | Only with `AUTH_MODE=gateway`: `{"error": {"code": "FORBIDDEN", "message": "…"}}` if the caller is not an organiser or staff |
| 404 Not Found | `{"error": {"code": "TICKET_NOT_FOUND", "message": "…"}}` if the signature is genuine but no such ticket exists |
| 409 Conflict | `{"error": {"code": "TICKET_ALREADY_CHECKED_IN", "message": "…", "details": {"checked_in_at": "2026-11-20T18:15:02Z"}}}`, `{"error": {"code": "TICKET_INVALIDATED", "message": "…", "details": {"invalidation_reason": "EVENT_CANCELLED"}}}` or `{"error": {"code": "TICKET_EVENT_MISMATCH", "message": "…", "details": {"ticket_event_id": "…"}}}`. When two scanners submit the same ticket at once, exactly one gets 200 and the other gets `TICKET_ALREADY_CHECKED_IN` |

## Internal endpoints

Reachable only on the private service network; not exposed through API Gateway. Like Event Service's `/internal/v1`, these have no authentication and rely on security groups. Endpoint 7 makes Notification Service email a buyer again, so `/internal/v1` must never be routed publicly.

### 5. Get an order's tickets

| Field | Value |
| --- | --- |
| Endpoint | `GET /internal/v1/orders/{order_id}/tickets` |
| Parameters | Path: `order_id` (UUID, required). Used by Checkout or support for reconciliation, and by integration tests |
| Request body | None |
| Response type | application/json |
| 200 OK | `{"order_id": "e1f2a3b4-…", "user_id": "2f9c7d10-…", "event_id": "c4a1e8f2-…", "issuance_status": "ISSUED", "rejection_reason": null, "quantity": 2, "tickets": [{"ticket_id": "6f0d3c2a-…", "ticket_number": 1, "status": "VALID"}], "ticket_issued": {"message_id": "3c9b1f40-…", "status": "PUBLISHED", "attempts": 1, "last_attempt_at": "2026-11-20T12:06:17Z", "published_at": "2026-11-20T12:06:17Z", "last_error": null}, "received_at": "2026-11-20T12:06:16Z"}`. `issuance_status` is `ISSUED`, `INVALIDATED` (refunded, or event cancelled after issuance) or `REJECTED` (the confirmation arrived after the event was cancelled or the order refunded; `rejection_reason` is `EVENT_CANCELLED` or `ORDER_REFUNDED`, no tickets exist and `ticket_issued` is `null`). `ticket_issued` is the order's latest `TicketIssued` (a republish adds a newer one); its `status` is `PENDING` (not published yet), `PUBLISHED` (the broker confirmed it) or `CANCELLED` (the tickets were invalidated before it went out) |
| 400 Bad Request | `{"error": {"code": "VALIDATION_ERROR", "message": "…"}}` if `order_id` is not a UUID |
| 404 Not Found | `{"error": {"code": "ORDER_NOT_FOUND", "message": "…"}}` |

### 6. List outbox messages

| Field | Value |
| --- | --- |
| Endpoint | `GET /internal/v1/outbox-messages?status=PENDING&limit=50` |
| Parameters | Query: `status` (optional: `PENDING`, `PUBLISHED`, `CANCELLED`; default `PENDING`), `limit` (optional, default 50, integer 1–200) |
| Request body | None |
| Response type | application/json |
| 200 OK | For monitoring failed background work: `PENDING` messages with `attempts` above 1 are ones the broker refused or couldn't be reached for, with the last error. Newest first, up to `limit`; returns `items` and `count` rather than pages. `{"items": [{"message_id": "3c9b1f40-…", "message_type": "TicketIssued", "order_id": "e1f2a3b4-…", "status": "PENDING", "attempts": 3, "created_at": "2026-11-20T12:06:16Z", "last_attempt_at": "2026-11-20T12:06:51Z", "next_attempt_at": "2026-11-20T12:07:11Z", "published_at": null, "last_error": "AMQPConnectionError: …"}], "count": 1}` |
| 400 Bad Request | `{"error": {"code": "VALIDATION_ERROR", "message": "…"}}` for an invalid `status` or `limit` |

### 7. Republish an order's TicketIssued

| Field | Value |
| --- | --- |
| Endpoint | `POST /internal/v1/orders/{order_id}/ticket-issued/republish` |
| Parameters | Path: `order_id` (UUID, required). For support, when a buyer needs the ticket email again |
| Request body | None |
| Response type | application/json |
| 202 Accepted | Queues a new `TicketIssued` with a new `message_id` (so Notification Service doesn't drop it as a duplicate), carrying the order's tickets that are still valid. If one is still waiting to be published, that one is made due now and returned instead. `{"order_id": "e1f2a3b4-…", "message_id": "8a7e2d11-…", "status": "PENDING"}` |
| 400 Bad Request | `{"error": {"code": "VALIDATION_ERROR", "message": "…"}}` if `order_id` is not a UUID |
| 404 Not Found | `{"error": {"code": "ORDER_NOT_FOUND", "message": "…"}}` |
| 409 Conflict | `{"error": {"code": "TICKET_ISSUED_NOT_REPUBLISHABLE", "message": "…", "details": {"issuance_status": "INVALIDATED", "valid_tickets": 0}}}` if the order was rejected or invalidated, or every ticket has been checked in |

---

## Validation

| Field | Rule |
| --- | --- |
| `ticket_id` (path) | Required, UUID |
| `order_id` (path, query) | UUID |
| `event_id` (query) | UUID |
| `status` (list tickets) | `VALID`, `CHECKED_IN` or `INVALIDATED` |
| `page` | Integer ≥ 1 |
| `page_size` | Integer 1–100 |
| `scale` | Integer 2–20 |
| `X-User-Id` | Required on buyer routes, non-blank, max 64 characters |
| `qr_payload` (check-in) | Required, max 500 characters, `TKT1.<key_id>.<ticket_id>.<signature>` with a genuine signature |
| `event_id` (check-in) | Required, UUID |
| `status` (outbox messages) | `PENDING`, `PUBLISHED` or `CANCELLED` |
| `limit` | Integer 1–200 |

The five-ticket-per-buyer limit is enforced by Reservation Service; Ticket accepts `quantity` 1–5 in `OrderConfirmed`.

---

## RabbitMQ messages

Ticket Service consumes four messages and publishes one, [`TicketIssued`](#ticketissued-published). The team broker (rabbitmq-service) owns the whole topology, generated from its `topology/messages.yaml`: each publisher has a durable topic exchange, and each consumer of a message gets its own quorum queue `<consumer>.<Message>`. Ticket declares no queues; its broker user `ticket-service` may consume only its own queues and publish only `TicketIssued` (routing key `ticket.issued`) to its own exchange, `ticket.events`.

| Queue (quorum, owned by the broker) | Exchange | Routing key | Message |
| --- | --- | --- | --- |
| `ticket-service.OrderConfirmed` | `checkout.events` | `order.confirmed` | `OrderConfirmed` |
| `ticket-service.EventCancelled` | `event.events` | `event.cancelled` | `EventCancelled` |
| `ticket-service.RefundCompleted` | `checkout.events` | `refund.completed` | `RefundCompleted` |
| `ticket-service.EventPublished` | `event.events` | `event.published` | `EventPublished` |

If a queue is missing (for example while the broker is being redeployed), Ticket's worker logs a warning, keeps consuming its other queues and checks again every minute.

Consumed messages are delivered at least once. Ticket acknowledges a message only after its database transaction commits, records each `message_id` in `processed_messages` in that same transaction (kept 30 days), and relies on database uniqueness (`order_id`, and `(order_id, ticket_number)`) so the same order arriving under a different `message_id` creates no extra tickets. `OrderConfirmed`, `EventCancelled` and `RefundCompleted` for one event lock that event's row first, so they apply one after another and whichever commits first decides the outcome.

Each queue dead-letters through the broker's `ticketto.dlx` to `<queue>.dlq`. A malformed message (bad JSON, missing required fields, a `schema_version` other than 1) is rejected straight to the DLQ. A message whose handling fails is rejected with requeue (`basic_reject`, never `basic_nack`, which the broker's RabbitMQ 4.3 does not count) and dead-lettered after 5 redeliveries (the broker's `delivery-limit` policy). Message types Ticket does not use are acknowledged and ignored.

The envelope is the broker's (`message_id`, `message_type`, `schema_version`, `occurred_at`, `producer`, `payload`; `contracts/envelope.schema.json` in rabbitmq-service). For compatibility with the Technical Contract tab, `event_type` is accepted in place of `message_type` and `data` in place of `payload`. `correlation_id` (envelope or AMQP property) is optional, and unknown extra fields are ignored.

### OrderConfirmed

Exchange: `checkout.events`. Routing key: `order.confirmed`. Queue: `ticket-service.OrderConfirmed`. Publisher: Checkout. Consumers: Ticket, Queue.

```json
{
  "message_id": "5d3c2b1a-7e8f-4a90-b1c2-d3e4f5a6b7c8",
  "message_type": "OrderConfirmed",
  "schema_version": 1,
  "occurred_at": "2026-11-20T12:06:15Z",
  "producer": "checkout-service",
  "correlation_id": "6f1c2a90-3b1e-4c55-9d1a-2f0e8a7b4c11",
  "payload": {
    "order_id": "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b",
    "hold_id": "b1e2d3c4-5f60-4718-92a3-b4c5d6e7f809",
    "admission_id": "8b0e5c1a-4f2d-4c1e-9a77-2d6f0b3e9c41",
    "user_id": "2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33",
    "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
    "category_id": "b05e8625-6d43-4252-ad2c-bda3222a769b",
    "quantity": 2,
    "delivery_email": "buyer@example.com",
    "confirmed_at": "2026-11-20T12:06:15Z"
  }
}
```

| Field | Rule |
| --- | --- |
| `order_id`, `event_id`, `category_id` | Required, UUID |
| `hold_id`, `admission_id` | Optional, UUID. Queue and Reservation use them; Ticket only records them, and a missing or malformed value never rejects the order |
| `user_id` | Required; the buyer's Cognito subject (`X-User-Sub`), which Checkout records. Before `AUTH_MODE=gateway` it is the `X-User-Id` buyers send |
| `quantity` | Required, integer 1–5: the number of admissions in the confirmed hold, which Checkout copies from Reservation. Ticket issues exactly this many tickets, one QR code each |
| `delivery_email` | Delivery email snapshot (`customer_email` or `email` also accepted). If missing or invalid, tickets are still issued and `TicketIssued` carries `delivery_email: null` |
| `confirmed_at` | Optional |

Ticket's handling: lock the event's row; if tickets already exist for `order_id`, do nothing; if the event is cancelled, or a `RefundCompleted` for this order was already processed, record the order as `REJECTED` and issue nothing; otherwise create `quantity` `VALID` tickets, each with a random `ticket_id` and signed QR payload, and write a pending `TicketIssued` to the outbox, all in one transaction. A delayed confirmation can therefore never create a valid ticket after cancellation.

### RefundCompleted

Exchange: `checkout.events`. Routing key: `refund.completed`. Queue: `ticket-service.RefundCompleted`. Publisher: Checkout. Consumers: Ticket. Checkout sends it once per order, only after a full refund succeeded and the booking was cancelled.

```json
{
  "message_id": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
  "message_type": "RefundCompleted",
  "schema_version": 1,
  "occurred_at": "2026-11-20T13:00:00Z",
  "producer": "checkout-service",
  "payload": {
    "order_id": "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b",
    "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
    "refund_id": "0b7c6d5e-4f3a-4b2c-9d1e-8f7a6b5c4d3e",
    "amount": 57600,
    "currency": "SGD"
  }
}
```

`order_id` and `event_id` are required; `refund_id`, `amount` (or `refund_amount`) and `currency` are stored, and anything else is ignored. Ticket records the refund against the order (so a late `OrderConfirmed` is rejected), invalidates the order's `VALID` tickets with reason `ORDER_REFUNDED`, and cancels the order's `TicketIssued` if it hasn't been published yet.

### EventCancelled

Exchange: `event.events`. Routing key: `event.cancelled`. Queue: `ticket-service.EventCancelled`. Publisher: Event. Consumers: Queue, Reservation, Checkout, Ticket.

```json
{
  "message_id": "9b2e4f71-1c0a-4d3e-8f6b-5a7c2d9e0f13",
  "message_type": "EventCancelled",
  "schema_version": 1,
  "occurred_at": "2026-11-19T09:30:00Z",
  "producer": "event-service",
  "payload": {
    "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
    "event_version": 3,
    "cancellation_id": "0fd4a84a-f4af-4a25-8ead-00430618aaaa",
    "cancelled_at": "2026-11-19T09:30:00Z",
    "reason": "Venue closure"
  }
}
```

`event_id` is required. Ticket stores the cancellation locally, invalidates every `VALID` ticket for the event with reason `EVENT_CANCELLED`, and cancels the event's `TicketIssued` messages that haven't been published yet. Invalidation does not wait for refunds. A cancellation takes precedence over any earlier or later `EventPublished` for the same event, even if the messages arrive out of order.

### EventPublished

Exchange: `event.events`. Routing key: `event.published`. Queue: `ticket-service.EventPublished`. Publisher: Event. Consumers: Queue, Reservation, Ticket.

Ticket reads `event_id`, `event_version`, `title`, `starts_at`, `venue` and `ticket_categories[].category_id` / `name` from the payload to cache the event snapshot shown on tickets and sent in `TicketIssued`. Updates apply only for a higher `event_version`, never reopen a cancelled event, and skip a malformed category entry rather than rejecting the message. Ticket issuance does not depend on this message; without it, Ticket fetches the details once from Event Service when `EVENT_SERVICE_URL` is set. Ticket also accepts `EventUpdated` with the same fields, should Event publish it.

### TicketIssued (published)

Exchange: `ticket.events`. Routing key: `ticket.issued`. Publisher: Ticket. Consumers: Notification, which emails the buyer their tickets (its queue `notification-service.TicketIssued` exists once rabbitmq-service lists it as a consumer; until then the broker keeps the messages in `ticketto.unrouted`).

One message per issued order, carrying every ticket of the order that is still valid. It is published after the tickets are committed: never for a `REJECTED` order, and not at all if the tickets are invalidated before it goes out.

```json
{
  "message_id": "3c9b1f40-7d2e-4a8b-9c61-5e0f2a4b8d17",
  "message_type": "TicketIssued",
  "schema_version": 1,
  "occurred_at": "2026-11-20T12:06:16Z",
  "producer": "ticket-service",
  "payload": {
    "order_id": "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b",
    "user_id": "2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33",
    "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
    "delivery_email": "buyer@example.com",
    "title": "The Midnight Symphony",
    "venue": "Victoria Theatre & Concert Hall",
    "starts_at": "2026-11-21T11:30:00Z",
    "category_name": "CAT 1",
    "tickets": [
      {
        "ticket_id": "6f0d3c2a-8b1e-4f7a-9c5d-2e4b6a8c0d1f",
        "ticket_number": 1,
        "qr_payload": "TKT1.k1.6f0d3c2a-8b1e-4f7a-9c5d-2e4b6a8c0d1f.dGhpcy1pcy1hbi1leGFtcGxlLXNpZ25hdHVyZQ"
      }
    ]
  }
}
```

| Field | Rule |
| --- | --- |
| `order_id`, `user_id`, `event_id` | Always present. `user_id` is the buyer's Cognito subject, as in `OrderConfirmed` |
| `delivery_email` | The delivery email snapshot from `OrderConfirmed`, or `null` if it had none or an invalid one |
| `title`, `venue`, `starts_at`, `category_name` | The cached event snapshot (`EventPublished`, else Event Service). `null` when Ticket doesn't know them; never guessed |
| `tickets` | At least one entry, ordered by `ticket_number`. `qr_payload` is the same signed payload the buyer sees in the app (see QR payload); render it as the QR code |

How Ticket publishes it (rabbitmq-service `docs/MESSAGING.md`):

- **Transactional outbox.** The `OrderConfirmed` transaction that creates the tickets also writes a pending `outbox_messages` row with the `message_id`. The worker publishes due rows every `OUTBOX_POLL_SECONDS` (1 s), building the payload from the order's tickets that are still valid.
- **Publish rules.** Persistent (`delivery_mode=2`), with publisher confirms and `mandatory=True`. AMQP properties: `message_id`, `type` = `TicketIssued`, `correlation_id` (the `OrderConfirmed`'s, or the republish request's `X-Correlation-Id`) and `content_type` = `application/json`. The envelope has no `correlation_id` field, as the broker's envelope schema allows no extra fields.
- **Retries.** A row is marked `PUBLISHED` only after the broker confirms. If the broker refuses or is unreachable, the row stays `PENDING` and is retried with backoff (5 s doubling, capped at 5 minutes; `OUTBOX_RETRY_BASE_SECONDS`, `OUTBOX_RETRY_MAX_SECONDS`) until it succeeds. Endpoint 6 lists the rows still waiting.
- **Duplicates.** A retry after a crash reuses the same `message_id`, so consumers must deduplicate on it. Endpoint 7 publishes the order again under a new `message_id`, for a deliberate resend.
- **Cancellation.** `EventCancelled` or `RefundCompleted` before the message is published sets it to `CANCELLED`. One already published stays `PUBLISHED`: Notification hears about the cancellation or refund itself.

---

## Health checks

Not exposed through API Gateway.

### 8. Liveness check

| Field | Value |
| --- | --- |
| Endpoint | `GET /health` |
| Parameters | None |
| Request body | None |
| Response type | application/json |
| 200 OK | `{"status": "ok", "service": "ticket-service"}` if the process is running |

### 9. Readiness check

| Field | Value |
| --- | --- |
| Endpoint | `GET /ready` |
| Parameters | None |
| Request body | None |
| Response type | application/json |
| 200 OK | `{"status": "ready"}` if the database is reachable |
| 503 Service Unavailable | `{"status": "not_ready", "failed": ["database"]}` otherwise |

---

## Ticket email

Notification Service sends the ticket email, so that each service has one job. Ticket gives it everything the email needs in [`TicketIssued`](#ticketissued-published): the delivery address, the event details and each ticket's QR payload. Ticket has no SMTP or SES settings. A failure to publish or to send never undoes the booking or the tickets, and buyers can always see their tickets with endpoints 1 to 3.

## Data ownership

Ticket Service owns the MySQL schema `db_ticket`.

| Table | Main stored data |
| --- | --- |
| `event_states` | One row per event seen: cancellation state, `event_version` and the cached snapshot (title, venue, start time, category names). Also the per-event lock |
| `ticket_orders` | One row per confirmed order received: user, event, category, quantity, delivery email snapshot and `issuance_status` |
| `tickets` | One row per ticket: `ticket_id`, order, `ticket_number` (unique per order), user, event, category, status, QR key ID and signature, check-in and invalidation fields |
| `check_ins` | One row per successful check-in, unique per ticket, with the scanner |
| `order_refunds` | Refunds received, so a delayed `OrderConfirmed` is rejected |
| `outbox_messages` | One row per message to publish (`TicketIssued`): `message_id`, order, status, attempts, next attempt, publish time and last error |
| `email_deliveries` | No longer written (the email moved to Notification Service). Kept so the previous image still runs after the migration; to be dropped later |
| `processed_messages` | `message_id` of every handled message, for deduplication (kept 30 days) |

## Points for other service owners

Ticket needs the following from other services; Ticket does not change their code.

1. rabbitmq-service: in `topology/messages.yaml`, list `notification-service` as a consumer of `TicketIssued` and set its status to `implemented`, add the publisher's schema `contracts/TicketIssued.payload.schema.json` (the payload above; Ticket owns it), then redeploy the broker. Ticket's user can already publish it. In AWS, Ticket's security group must be in the broker's `amqp_allowed_sg_ids`.
2. Checkout (done): publishes `OrderConfirmed` and `RefundCompleted` on `checkout.events` in the broker's envelope, with `quantity` from the hold and `user_id` from `X-User-Sub`. It must keep sending `RefundCompleted` only for a full refund that cancels the booking, because Ticket voids the whole order.
3. Event (done): `EventPublished` includes `venue`.
4. Frontend: a check-in screen for organisers that posts the scanned QR payload and the event to `POST /api/v1/tickets/check-in`.
5. Notification: consume `notification-service.TicketIssued`, deduplicate on `message_id`, and email the buyer one QR code per entry in `tickets` (rendered from `qr_payload`). When `delivery_email` is `null`, find the buyer's address by `user_id` or skip the email.
6. Technical Contract tab: its RabbitMQ section still names a single `ticketto.events` exchange and an `event_type` / `data` envelope; the team broker uses per-publisher exchanges and `message_type` / `payload` (rabbitmq-service's `docs/MESSAGING.md`). Ticket accepts both.
