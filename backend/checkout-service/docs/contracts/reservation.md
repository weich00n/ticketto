# Contract: checkout-service ↔ reservation-service

Checkout's side of the contract (consumer-driven): what checkout sends, what it accepts and
how it reacts to each case. Verified against reservation-service's code on 2026-10-22
(`InternalReservationController`, `ReservationService`, `ErrorCode`, `GlobalExceptionHandler`,
`OutboxWriter`) and against rabbitmq-service's `contracts/envelope.schema.json` and
`topology/messages.yaml`.

If reservation-service changes any of the shapes below, the tests named in each section fail.

## 1. HTTP (synchronous)

Client: `app/clients/reservation.py`. Base URL `RESERVATION_BASE_URL`; 10 s timeout; no auth
headers (internal routes, VPC only). JSON is snake_case both ways, and null fields are omitted.
Checkout forwards `X-Correlation-Id` when it has one.

### `GET /internal/v1/reservations/{hold_id}`: read the hold to price the order

Called by `POST /api/v1/orders`.

| Reservation answers | Checkout does | API response |
| --- | --- | --- |
| 200 (any status), same `user_id`, and an order already exists for the hold | returns that order: once paid, the hold reads `CONFIRMED`, and a repeated request is still idempotent | 200 |
| 200, `status: ACTIVE`, same `user_id` as `X-User-Sub` | creates the order from the hold's figures (quantity, amounts, currency, deadlines) | 201 / 200 |
| 200 (any status), other `user_id` | nothing; ownership is checked before status, so an ended hold's state doesn't leak | 404 `HOLD_NOT_FOUND` (never 403) |
| 200, `status: EXPIRED` | nothing | 409 `HOLD_EXPIRED` |
| 200, `status: CANCELLED` | nothing | 409 `EVENT_CANCELLED` |
| 200, `status: CONFIRMED` | nothing | 409 `ORDER_STATE_CONFLICT` |
| 404 `HOLD_NOT_FOUND` | nothing | 404 `HOLD_NOT_FOUND` |
| 503 / 500 / timeout / connection error | nothing | 503 `RESERVATION_UNAVAILABLE` |
| 200 missing a required field, or any other 4xx | logged as a contract error | 409 `ORDER_STATE_CONFLICT` |

Required fields of `InternalHoldView`: `hold_id, user_id, event_id, status, quantity,
unit_amount, total_amount, currency, payment_expires_at, hold_expires_at, server_time`.
Optional fields: `category_id, admission_id, order_id`.

### `POST /internal/v1/reservations/{hold_id}/confirm`: confirm after a verified payment

Body: `{"order_id": "<checkout order id>", "payment_reference": "<Stripe pi_…>"}`. Both fields are
`@NotBlank`; checkout never sends a blank one. A paid order with no intent id goes to
`RECONCILIATION` instead.

Called by the `payment_intent.succeeded` webhook and by the worker's confirm-retry sweep
(every ~10 s, for `CONFIRMING` orders whose payment succeeded).

| Reservation answers | Checkout does | Booking |
| --- | --- | --- |
| 200, `duplicate: false` or `true` | emits `OrderConfirmed` | `CONFIRMED` |
| 409 `HOLD_EXPIRED` / `EVENT_CANCELLED` / other 409 | records the refund obligation | `CANCELLED` |
| 409 `ORDER_MISMATCH` | flags it for a person; no refund | `RECONCILIATION` |
| 404 `HOLD_NOT_FOUND` / `EVENT_NOT_FOUND` | flags it for a person; no refund | `RECONCILIATION` |
| 503 `DEPENDENCY_UNAVAILABLE` / 500 / timeout | nothing; the sweep retries it | stays `CONFIRMING` |
| 400 `INVALID_REQUEST`, or a 200 missing a field | logs a contract error; the sweep retries it | stays `CONFIRMING` |

Why:
- **503 always means retry.** Reservation answers 503, not `HOLD_EXPIRED`, while another
  confirmation for the hold is in flight, precisely so checkout never refunds a payment that is
  about to be accepted.
- **A 404 is not an expiry** (decision D1). It means reservation has no record matching the hold
  the order was priced from, so a person decides what happens to the money.
- **A 400 is checkout's own bug.** It must never refund the buyer.
- **One bad order can't stall the sweep.** Each order in the sweep is isolated.

Tests: `tests/unit/test_reservation_client.py` covers every row above as reservation's exact
bodies; `tests/integration/test_reservation_contract.py` covers checkout's reaction to each.
`tests/integration/test_flow_2.py` checks both calls against the real service.

## 2. Messages (asynchronous)

Every inbound message must be the broker's six-field envelope (decision D3). Checkout does not
accept other dialects:

```json
{ "message_id": "<uuid>", "message_type": "RefundRequired", "schema_version": 1,
  "occurred_at": "2026-10-21T09:10:00Z", "producer": "reservation-service", "payload": { } }
```

- `schema_version` is an **integer**, and only `1` is accepted.
- `occurred_at` is UTC and ends in `Z`.
- `producer` matches `*-service`.
- No extra top-level fields are allowed; `correlation_id` goes in the AMQP property, not the body.
- AMQP properties: `message_id`, `type` (must equal `message_type`), `correlation_id`,
  `content_type: application/json`, persistent.
- A message only goes to its own queue: a `message_type` that doesn't match the queue is
  dead-lettered.

Payload fields checkout requires are listed below (`app/messaging/payloads.py`). Every other
field is ignored. A missing or wrong-typed required field is dead-lettered at once (rejected
without requeue), not redelivered five times.

| Queue (source) | Required payload | Effect |
| --- | --- | --- |
| `checkout-service.RefundRequired` (`reservation.events`, `refund.required`) | `order_id`, `hold_id`, `total_amount` (int), `currency` | Refunds the order once and cancels the booking (`CONFIRMED` too). If `hold_id` differs from the order's, the message is dead-lettered and nothing changes. If the amount or currency differs, a warning is logged and the order's own amount is refunded. An unknown `order_id` is acked and logged. |
| `checkout-service.ReservationExpired` (`reservation.events`, `reservation.expired`) | `hold_id` | Expires the order if it's still `PENDING_PAYMENT` / `CONFIRMING` and unpaid, and cancels the intent. Any other state is left alone. |
| `checkout-service.EventCancelled` (`event.events`, `event.cancelled`, from event-service) | `event_id` | Unpaid orders → `CANCELLED` and the intent is cancelled. Paid orders, **`CONFIRMED` included** (decision D2) → refund, then `CANCELLED`. |

Duplicates are ignored by `message_id`, recorded in the same transaction as the effect.
`EventCancelled` followed by `RefundRequired` for the same order (in either order) produces
exactly one refund.

Fixtures: `tests/contracts/reservation.RefundRequired.json` is the exact message reservation's
relay must publish; `reservation.RefundRequired.invalid.json` holds known-wrong shapes that must
be dead-lettered; `reservation.ReservationExpired.json` is the minimum checkout needs.

Tests:
- `tests/unit/test_message_contracts.py` validates the fixtures and checkout's own outbound
  envelopes against the broker's schema and catalogue (when `../rabbitmq-service` is present).
- `tests/integration/test_reservation_contract.py` covers checkout's reactions.
- `tests/integration/test_broker.py` covers the live broker, including the DLQ.

## 3. Outbound messages reservation receives

None. `OrderConfirmed` (`checkout.events`, `order.confirmed`) goes to queue-service, ticket-service
and user-service; the broker no longer binds it to a reservation queue, because reservation learns
about the order from checkout's confirm call (section 1).

## 4. What only the other teams can change

Nothing is open. The gaps listed here earlier were closed by their owners (checked against
reservation-service and rabbitmq-service `main` on 2026-11-03):

- **Reservation's relay** publishes its outbox rows as the six-field envelope, including
  `RefundRequired` to `reservation.events` / `refund.required` (`OutboxPublisher`, every second).
  It is sent for a `CONFIRMED` hold when its event is cancelled, and for a hold whose confirmation
  commits as the event is cancelled. Checkout still refunds on `EventCancelled` too (D2); whichever
  arrives first creates the refund.
- **`ReservationExpired`** is written by `HoldExpiryWorker` when a hold's TTL passes.
- **`reservation-service.OrderConfirmed`** is no longer in the broker topology, so no queue grows.
- **Reservation's docs** no longer describe checkout's old envelope parser.
- **The broker catalogue** no longer mentions `orders.topic`, marks `RefundFailed` implemented, and
  has `RefundRequired` and `ReservationExpired` payload schemas.
