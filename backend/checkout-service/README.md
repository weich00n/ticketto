# Checkout Service

Turns a reservation hold into a paid, confirmed booking. Checkout reads the authoritative hold from Reservation Service, creates a Stripe PaymentIntent for the buyer to pay, and confirms the hold only when a signature-verified Stripe webhook says the payment succeeded — the browser's return page is never proof of payment. Refunds (expired holds, cancelled events) are recorded as obligations before they are requested from Stripe, so a retry can never double-refund.

Python 3.12, Flask, SQLAlchemy, Alembic, MySQL and RabbitMQ.

## Running locally

copy .env.example .env

```sh
docker compose up --build
```

This runs the migrations and starts:

- the API on `http://localhost:3002`
- the worker (`python -m app.worker`), which relays outbox messages, sweeps deadlines and consumes the three checkout queues
- MySQL on host port `3308` (database `checkout_service`) and the shared broker `ticketto-rabbitmq:local` (built from the sibling `../rabbitmq-service` repo, topology preloaded; AMQP `127.0.0.1:5673`, management UI `http://localhost:15673`)
- a reservation stub on host port `18081` (the real reservation-service needs Redis, MySQL and Queue's signing key; the stub answers the two `/internal/v1/reservations` endpoints checkout calls with the real service's bodies and error codes, for one fixed user `local-dev-user`). A hold id prefix picks one of the real service's other answers (`expired-`, `cancelled-`, `gone-`, `noevent-`, `busy-`); see `scripts/dev/reservation_stub.py`

The local compose stack carries throwaway credentials (marked `gitleaks:allow` in the file). To exercise the real payment path, put Stripe test-mode keys in a `.env` (`STRIPE_API_KEY`, `STRIPE_WEBHOOK_SECRET`, see `.env.example`); the API and worker boot fine with both empty (the webhook then verifies against the same throwaway local secret the tests use).

To run the API outside Docker:

```sh
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
export DATABASE_URL='mysql+pymysql://ticketto:ticketto-local@localhost:3308/checkout_service'
export RABBITMQ_URL='amqp://ticketto:ticketto-local@localhost:5673/%2F'
export RESERVATION_BASE_URL='http://localhost:18081'
alembic upgrade head
gunicorn --bind 0.0.0.0:3002 --workers 2 'app.main:create_app()'
```

## Configuration

| Variable | Description |
| --- | --- |
| `DATABASE_URL` | MySQL URL. Used when set; when unset, the URL is built from `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER` and `DB_PASSWORD` (the form AWS task definitions use) |
| `DB_SSL_CA` | Path to the RDS CA bundle (`/etc/ssl/rds/global-bundle.pem` in the image). Against RDS this keeps TLS on (verified); for non-RDS MySQL the driver instead disables TLS and uses the deterministic RSA exchange, which MySQL 8's `caching_sha2_password` requires |
| `RABBITMQ_URL` | Broker URL (vhost in the path, e.g. `…/5672/%2F`) |
| `PORT` | gunicorn bind port, default `3002` (set by the Dockerfile; the health endpoints always live at the root `/health` and `/ready`) |
| `LOG_LEVEL` | Default `INFO` |
| `RESERVATION_BASE_URL` | Reservation Service address, default `http://reservation-service:8081` |
| `STRIPE_API_KEY` | PaymentIntent key; empty is fine for boot, tests and the webhook |
| `STRIPE_WEBHOOK_SECRET` | Webhook verification secret; empty falls back to the same throwaway local test value the suite uses |
| `EXPIRY_SWEEP_SECONDS` | Worker deadline-sweep cadence, default `10` |

The three consumed broker queues are fixed by design (the broker owns the topology), not environment-driven.

## Authentication

API Gateway validates the Cognito token and forwards `X-User-Sub`; checkout treats that value as the buyer's platform `user_id` and compares it directly against the hold's `user_id` (a locked working assumption — the single mapping point is `app/auth.py::require_buyer`). Buyers see only their own orders; a foreign or unknown order is `404`, never `403`. The service must only be reachable through the gateway and from other services.

The exception is the Stripe webhook: Stripe calls `POST /webhooks/stripe` directly (no gateway, no `X-User-Sub`), and the `Stripe-Signature` header is the only authentication.

## API

| Method | Route | Access | Description |
| --- | --- | --- | --- |
| `GET` | `/health` | Public | Liveness |
| `GET` | `/ready` | Public | Database and broker reachable |
| `POST` | `/api/v1/orders` | Buyer | Create, or idempotently retrieve, the order for a hold; body `{hold_id, delivery_email}` (`customer_email` accepted as an alias), optional `Idempotency-Key`. Quantity and amounts come from the hold, never the body |
| `GET` | `/api/v1/orders?page=&page_size=` | Buyer | The buyer's own orders, paginated |
| `GET` | `/api/v1/orders/{order_id}` | Owner | One of the buyer's orders (foreign → 404) |
| `POST` | `/webhooks/stripe` | Stripe | `payment_intent.succeeded` / `payment_intent.payment_failed` / `charge.refunded`; signature is the auth |

The health routes are root-level (not under `/api/v1`) because the Dockerfile health check and the ALB target group call them by absolute path.

The payment flow: `POST /orders` reads the hold from Reservation Service (the price snapshot is authoritative — the frontend never sets amounts), stores the order as `PENDING_PAYMENT` with the hold's payment deadline, creates a Stripe PaymentIntent (idempotency key derived from the order) and returns its `client_secret` for the frontend's Payment Element. If Stripe is down at that moment the order is kept and the request answers 503; repeating it creates the missing intent. Checkout never self-confirms: the order moves `PENDING_PAYMENT → CONFIRMING` only on a signature-verified `payment_intent.succeeded` webhook, then confirms the hold, and `CONFIRMED` (plus the `OrderConfirmed` event) only when the confirm succeeds.

Order view fields: `order_id`, `status`, `hold_id`, `event_id`, `category_id`, `quantity`, `unit_amount`, `total_amount`, `currency`, `delivery_email`, `payment` (`status`, `amount`, `currency`, `succeeded_at`, `failed_at`), `refunds[]` (`refund_id`, `amount`, `currency`, `reason`, `status`, `stripe_refund_id`, `last_error`), `client_secret`, `created_at`, `updated_at`.

Fields are snake_case, times are UTC (`2026-10-20T10:00:00Z`), and money is an integer in minor units (`8800` = SGD 88.00).

State machines (Technical Contract §9):

- booking `PENDING_PAYMENT → CONFIRMING → CONFIRMED | EXPIRED | CANCELLED`. A cancellation takes precedence over an in-flight confirm (a paid-but-unconfirmed order is refunded, then cancelled); `CONFIRMED → CANCELLED` when the event is cancelled or Reservation reports a refund is owed (refunded once, whichever arrives first); `CONFIRMING → RECONCILIATION` when Reservation says the hold is confirmed for a different order, or does not know the hold or its event (404 on confirm), or the paid order has no payment intent id — flagged for a person, never auto-resolved and never auto-refunded.
- payment `PENDING → SUCCEEDED | FAILED | CANCELLED`. A success reported by Stripe is always recorded, even after a decline (`FAILED`) or a best-effort cancel (`CANCELLED`); if the booking has already ended by then, the payment is refunded.
- refund `REQUESTED → PENDING → SUCCEEDED | FAILED | REQUIRES_ACTION`, at most one per order. A refund stays `REQUESTED` until Stripe answers (outages are retried); `FAILED` is reported once and left for a person.

Errors:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "The request is invalid.",
    "details": { "delivery_email": "Must be a valid email address." }
  },
  "correlation_id": "4b0c…"
}
```

Codes: `VALIDATION_ERROR` (400), `UNAUTHENTICATED` (401), `HOLD_NOT_FOUND` / `ORDER_NOT_FOUND` (404), `HOLD_EXPIRED` / `EVENT_CANCELLED` / `ORDER_STATE_CONFLICT` / `IDEMPOTENCY_KEY_CONFLICT` (409), `STRIPE_UNAVAILABLE` / `RESERVATION_UNAVAILABLE` (503), `INTERNAL_ERROR` (500). The webhook additionally returns `400 BAD_SIGNATURE` for an unverifiable signature, and a `500` on transient failure — a 500 tells Stripe to retry.

Requests can send `X-Correlation-Id`; it is returned in the response, logged, and copied onto every message the request creates.

## Reservation-service contract

Checkout's side of the contract with reservation-service — every HTTP answer it handles and every message shape it accepts — is in [`docs/contracts/reservation.md`](docs/contracts/reservation.md), pinned by `tests/unit/test_reservation_client.py`, `tests/unit/test_message_contracts.py` (fixtures in `tests/contracts/`) and `tests/integration/test_reservation_contract.py`.

## Messages

The broker contract is owned by [rabbitmq-service](../rabbitmq-service) (`topology/messages.yaml`, `contracts/envelope.schema.json`, `docs/MESSAGING.md`).

Every state change and the message that describes it are saved in one transaction (the transactional outbox). The worker's relay publishes pending rows in commit order to checkout's own topic exchange, `checkout.events`, with the message's dotted routing key — persistent, mandatory, with publisher confirms — and marks a row delivered only once the broker has confirmed it. Checkout never declares exchanges or queues: the broker owns the topology. A message id is derived from the message type and what it is about (the order, or the refund), so the same fact is always the same message. Delivery is at least once: consumers deduplicate by `message_id` in the same transaction as the business effect.

Envelope (exactly six fields, `additionalProperties: false`; the AMQP `type` property equals `message_type`, and the correlation id travels as the AMQP `correlation_id` property, not in the body):

```json
{
  "message_id": "5d3c2b1a-7e8f-4a90-b1c2-d3e4f5a6b7c8",
  "message_type": "OrderConfirmed",
  "schema_version": 1,
  "occurred_at": "2026-10-20T08:00:00Z",
  "producer": "checkout-service",
  "payload": {}
}
```

Published (the worker emits all four; consumers as in rabbitmq-service's `topology/definitions.json`):

| Message | Routing key | `payload` fields | Consumed by |
| --- | --- | --- | --- |
| `OrderConfirmed` | `order.confirmed` | `order_id`, `hold_id`, `admission_id`, `user_id`, `event_id`, `category_id`, `quantity`, `delivery_email`, `total_amount`, `currency`, `confirmed_at` | ticket-service, queue-service, user-service |
| `RefundCompleted` | `refund.completed` | `order_id`, `event_id`, `refund_id`, `amount`, `currency` | ticket-service |
| `OrderRefunded` | `order.refunded` | `order_id`, `user_id`, `event_id`, `refund_id`, `amount`, `currency`, `reason` | notification-service |
| `RefundFailed` | `refund.failed` | same as `OrderRefunded`, plus `last_error` | notification-service |

`OrderRefunded` is emitted only when Stripe actually refunded (never at refund-request time). A message no queue is bound to is parked by the broker's `events-ae` policy in the durable `ticketto.unrouted` queue. `PaymentCompleted` was dropped for that reason: it had no consumer, so every confirmed order added one message there that nothing ever read.

Consumed (one pika consumer thread per queue, manual ack, prefetch 50). A message that can never be processed — a malformed envelope, a message type that does not belong on the queue (or disagrees with the AMQP `type`), or a payload missing a field checkout needs (`app/messaging/payloads.py`) — is rejected without requeue (straight to its `.dlq`); a transient failure is rejected with requeue, and the broker dead-letters it after 5 redeliveries. Checkout never uses `basic_nack`, which on RabbitMQ 4.3 bypasses that limit.

| Queue | Bound from | Effect |
| --- | --- | --- |
| `checkout-service.EventCancelled` | `event.events`, `event.cancelled` | Unpaid orders of the cancelled event → `CANCELLED`, and their payment intents are cancelled; paid ones, `CONFIRMED` included → refund, then cancel (cancellation beats an older payment success; checkout does not wait for reservation's `RefundRequired`: whichever of the two arrives first creates the refund, and the other is a no-op) |
| `checkout-service.ReservationExpired` | `reservation.events`, `reservation.expired` | Unpaid orders of the expired hold → `EXPIRED`, intent cancelled. Published by reservation when a hold's TTL passes; the worker's deadline sweep still expires unpaid orders on its own if the message is lost or late |
| `checkout-service.RefundRequired` | `reservation.events`, `refund.required` | Record the order's refund (at most one per order), cancel the booking (a `CONFIRMED` one too) and drive the Stripe refund to completion. The message's `hold_id` must match the order's (otherwise dead-lettered for a person); a differing amount is logged and the order's own amount is refunded |

The worker process (`python -m app.worker`, same image as the API) runs the outbox relay every ~2 s, the payment-deadline sweep every ~10 s, a confirm retry for stuck `CONFIRMING` orders every ~10 s, a refund processor every ~30 s, plus the three consumer threads; it drains the consumers on SIGTERM before exiting.

## Tests

```sh
ruff check . && ruff format --check .
python -m pylint app tests alembic
pytest                                # 89 unit tests (2 of them need ../rabbitmq-service checked out, else skipped)
pytest -m integration                 # 59 in-process behavioral tests (Flask test client + fakes, no services needed)
docker compose run --rm tests         # the 36-test in-process suite against the live compose stack
```

A live-broker suite proves what the in-process tests can't — that the relay publishes to the real broker, that the consumers drain the real queues, and that message-id dedupe holds over real redelivery:

```sh
docker compose up -d
pytest tests/integration/test_broker.py -m broker
```

It runs from the host venv against the compose stack's host ports (`:3002`, `:3308`, `:5673`); the test file carries working on-disk defaults, so no credential env vars are needed. Stripe is network-isolated: the payment-success webhook is hand-signed with stdlib HMAC (exactly what the SDK verifies) and orders are seeded straight into MySQL, so the only external call the suite makes is to the reservation stub.

`tests/integration/test_flow_2.py` (also `-m broker`) runs the same handoff against the **real** reservation-service instead of the stub: it seeds inventory, creates a hold with an HS256 admission token, buys it through checkout, and checks that reservation shows the hold `CONFIRMED` for that order and that `OrderConfirmed` reaches `checkout.events`. It is skipped unless `IT_RESERVATION_URL` and `IT_ADMISSION_TOKEN_SECRET` are set; the file's docstring has the setup.

### Contract tests

`tests/contract` checks checkout against the AMQP message contracts owned by rabbitmq-service.
The tests are generated by rabbitmq-service's `ticketto-contracts` package, from its catalogue:

- **Publishing** (`OrderConfirmed`, `OrderRefunded`, `RefundCompleted`, `RefundFailed`): the
  exchange, routing key, `mandatory`, publisher confirms, the AMQP properties, the envelope, and
  the payload against its schema. `tests/contract/conftest.py` takes an order through the API,
  a Stripe payment, an event cancellation and a refund (succeeded or failed). It runs the real
  relay, `Outbox.deliver_pending`, with only its broker connection replaced.
- **Consuming** (`EventCancelled`, `ReservationExpired`, `RefundRequired`): the worker consumes
  exactly checkout's queues. The real consumer path (`_dispose` plus the handler) acks a body with
  only the publisher's required fields, ignores unknown fields, and dead-letters a body missing a
  required field.

No broker or database server is needed: the tests use the in-memory SQLite database and the
reservation and Stripe fakes.

The package is pinned in `requirements-contract.txt`. It comes from rabbitmq-service's GitLab
package registry; if the registry is private, you need a GitLab personal access token
(`read_api`):

```sh
export CONTRACTS_INDEX_USER=__token__ CONTRACTS_INDEX_TOKEN=<your token>
pip install '.[dev,contract]'
pip install --no-deps -r requirements-contract.txt
pytest tests/contract -m contract
```

Without the package the tests are skipped, so `pytest` and the other jobs don't need it. To move
to a new contract version, change `ticketto-contracts==X.Y.Z` in `requirements-contract.txt`.
rabbitmq-service's `contracts/CHANGELOG.md` says what changed.

In CI, `contract-tests` runs them on every pipeline. rabbitmq-service also triggers a pipeline
here (on `main`) for every contract merge request that affects checkout-service, with
`CONTRACTS_VERSION` set to that merge request's pre-release. Those pipelines run only
`contract-tests`, never build or deploy, and a failure blocks the contract change.

## Deployment

The GitLab pipeline runs lint, pylint, unit and in-process integration tests (MySQL 8.4 + RabbitMQ 4.3.6 as CI services) on every merge request, builds the image, and — once the `DEPLOY_ENABLED` CI/CD variable is `true` — deploys `main` to ECS on Fargate as two services from one image. The AWS setup (three Secrets Manager secrets, RDS, ECR, OIDC, CI/CD variables) is in [`deploy/README.md`](deploy/README.md).
