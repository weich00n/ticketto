# Ticket Service

Issues one signed QR ticket per purchased admission, lets buyers view their tickets, lets venue staff check each valid ticket in once, and invalidates tickets when an event is cancelled or an order refunded. Events, inventory, orders, payments and refunds belong to the other services; Ticket learns about them only from RabbitMQ messages. The ticket email is notification-service's: Ticket publishes `TicketIssued` with everything it needs, QR payloads included.

Python 3.12, Flask, SQLAlchemy, Alembic, MySQL and RabbitMQ. Full API and message contract: [`docs/API.md`](docs/API.md).

## Running locally

```sh
docker compose up --build
```

This runs the migrations and starts:

- the API on `http://localhost:3006`
- the worker, which consumes RabbitMQ messages and publishes `TicketIssued`
- MySQL on port `3311`
- RabbitMQ 4.3.6 on `5672`, the team broker's version (UI at `http://localhost:15672`, user `ticketto`, password `ticketto-local`; there is no `guest` user). `broker-setup` then imports Ticket's queues and creates the `ticket-service` user with the team broker's permissions ([`scripts/broker`](scripts/broker)), so the worker runs exactly as it will in AWS.

If another service's stack is already using RabbitMQ on `5672`, stop it. To use the team's full broker instead, run `docker compose up` in rabbitmq-service, drop `rabbitmq` and `broker-setup` here, join its `ticketto` network and set `RABBITMQ_URL=amqp://ticket-service:ticketto-local@rabbitmq:5672/%2F`.

To try it end to end, send the messages Checkout and Event would send (as the broker's admin user):

```sh
eval "$(python scripts/dev/send_test_message.py order --quantity 2)"
curl -H "X-User-Id: $USER_ID" "localhost:3006/api/v1/tickets?order_id=$ORDER_ID"
python scripts/dev/send_test_message.py --help   # also: event, cancel, refund, poison
```

With no notification-service here, nothing consumes `TicketIssued`, so the broker keeps each one in `ticketto.unrouted`: open it in the RabbitMQ UI (**Queues → ticketto.unrouted → Get messages**) to see the message with the QR payloads.

To run the API outside Docker:

```sh
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env   # then edit; load it with: set -a; source .env; set +a
alembic upgrade head
flask --app 'app.main:create_app()' run --port 3006
python -m app.worker   # in a second terminal
```

## Configuration

| Variable | Description |
| --- | --- |
| `DATABASE_URL` | MySQL URL. Used locally; when unset, the URL is built from `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER` and `DB_PASSWORD` |
| `DB_SSL_CA` | Path to the RDS CA bundle (`/etc/ssl/rds/global-bundle.pem` in the image). Enables TLS |
| `RABBITMQ_URL` | RabbitMQ URL (worker only), as Ticket's own broker user `ticket-service`. In AWS: the `url` key of the broker's `rabibi/service/ticket-service` secret |
| `QR_SIGNING_KEYS` | Required. `key_id:secret[,key_id:secret…]`, secrets at least 32 characters. Never commit real values |
| `QR_ACTIVE_KEY_ID` | Key used for new tickets. Default: the first key listed |
| `OUTBOX_POLL_SECONDS` | How often the worker publishes pending `TicketIssued` messages. Default `1` |
| `OUTBOX_RETRY_BASE_SECONDS`, `OUTBOX_RETRY_MAX_SECONDS` | Backoff when the broker refuses a publish: default 5 s doubling, capped at 300 s. It never gives up |
| `EVENT_SERVICE_URL` | Event Service's private address. Optional; fills in event details missing from messages before `TicketIssued` is published |
| `AUTH_MODE` | `none` (default): no authentication yet, buyers send `X-User-Id`. `gateway`: trust API Gateway's Cognito headers and require one of `CHECK_IN_GROUPS` for check-in |
| `CHECK_IN_GROUPS` | Cognito groups allowed to check tickets in with `AUTH_MODE=gateway`, comma-separated. Default `organiser,staff` (the team's roles are `buyer` and `organiser`) |
| `PORT` | API port, default `3006` |
| `LOG_LEVEL` | Default `INFO` |

**Rotating the QR key:** add the new key first in the list (`k2:<new>,k1:<old>`) and deploy. New tickets use `k2`; tickets signed with `k1` keep working. Remove `k1` only after its events are over.

## Authentication

There is no authentication yet. Buyers identify themselves with an `X-User-Id` header (as with Queue Service), which must match the `user_id` Checkout sends in `OrderConfirmed`; buyers only see their own tickets, and anyone else's return `404`. Check-in is open to any caller.

When API Gateway and Cognito are added, set `AUTH_MODE=gateway`: the service then reads `X-User-Sub`, `X-User-Username` and `X-User-Groups` from the gateway, ignores `X-User-Id`, and check-in needs one of `CHECK_IN_GROUPS` (`organiser` or `staff` by default). The service must then only be reachable through API Gateway and from other services. `/internal/v1`, `/health` and `/ready` never get a public route.

## API

| Method | Route | Access | Description |
| --- | --- | --- | --- |
| `GET` | `/health` | Network | Liveness |
| `GET` | `/ready` | Network | Database readiness |
| `GET` | `/api/v1/tickets?event_id=&order_id=&status=&page=&page_size=` | Buyer (`X-User-Id`) | Own tickets, newest first |
| `GET` | `/api/v1/tickets/{ticket_id}` | Buyer (`X-User-Id`) | One ticket, with its QR payload |
| `GET` | `/api/v1/tickets/{ticket_id}/qr?scale=` | Buyer (`X-User-Id`) | QR code as PNG |
| `POST` | `/api/v1/tickets/check-in` | Open (organiser or staff with `AUTH_MODE=gateway`) | `{"qr_payload": "…", "event_id": "…"}`; admits each ticket once |
| `GET` | `/internal/v1/orders/{order_id}/tickets` | Services | Issuance status of an order |
| `GET` | `/internal/v1/outbox-messages?status=&limit=` | Services | Messages to publish by status (default `PENDING`: not published yet, with the last error) |
| `POST` | `/internal/v1/orders/{order_id}/ticket-issued/republish` | Services | Publish an order's `TicketIssued` again (a new `message_id`), so the buyer is emailed again |

Errors use the shared shape, with `correlation_id`:

```json
{ "error": { "code": "TICKET_ALREADY_CHECKED_IN", "message": "This ticket has already been used.", "details": { "checked_in_at": "2026-11-20T18:15:02Z" } }, "correlation_id": "4b0c…" }
```

## Messages

The team broker ([rabbitmq-service](https://gitlab.com/cs302-2026/g2-team5/backend/rabbitmq-service)) owns every exchange, queue and retry rule, from its `topology/messages.yaml`. Ticket declares no queues: its broker user may only read its own `ticket-service.<Message>` queues, and publish `TicketIssued` to its own exchange. The worker consumes:

| Queue (quorum, owned by the broker) | Exchange | Routing key | Effect |
| --- | --- | --- | --- |
| `ticket-service.OrderConfirmed` | `checkout.events` | `order.confirmed` | Issue `quantity` tickets and queue `TicketIssued` |
| `ticket-service.EventCancelled` | `event.events` | `event.cancelled` | Invalidate the event's tickets |
| `ticket-service.RefundCompleted` | `checkout.events` | `refund.completed` | Invalidate the order's tickets |
| `ticket-service.EventPublished` | `event.events` | `event.published` | Cache title, venue, time and category names for tickets and `TicketIssued` |

If a queue is missing, the worker logs a warning, keeps consuming the others and checks again every minute; it does the same if the broker deletes a queue it was consuming.

And it publishes one message:

| Message | Exchange | Routing key | When |
| --- | --- | --- | --- |
| `TicketIssued` | `ticket.events` | `ticket.issued` | Once per issued order, with each valid ticket's QR payload. notification-service emails the buyer from it |

`TicketIssued` goes through a transactional outbox: the transaction that issues the tickets writes a pending `outbox_messages` row, and the worker publishes it with publisher confirms, marking it `PUBLISHED` only once the broker has it and retrying with backoff until then. Retries keep the same `message_id`, so consumers deduplicate on it. If the tickets are invalidated before it goes out, it is `CANCELLED`. Payload and rules: [`docs/API.md`](docs/API.md#ticketissued-published).

Each queue dead-letters to `<queue>.dlq` through the broker's `ticketto.dlx`. Malformed messages are rejected straight there; others are retried with `basic_reject` (never `basic_nack`, which the broker's RabbitMQ doesn't count) and dead-lettered after 5 redeliveries. Handlers are idempotent (`processed_messages`, plus unique `order_id` and `(order_id, ticket_number)`), and messages for the same event lock that event's row, so a cancellation and a confirmation can never both win.

## How the important guarantees are enforced

| Guarantee | Mechanism |
| --- | --- |
| One ticket per admission, never duplicated | Unique `(order_id, ticket_number)`; `processed_messages`; duplicate order check under the event lock |
| No valid ticket after cancellation or refund | Same per-event row lock for `OrderConfirmed`, `EventCancelled` and `RefundCompleted`; late confirmations are recorded as `REJECTED`; refunds stored so a later confirmation is rejected |
| Each ticket checked in once | Conditional `UPDATE … WHERE status = 'VALID'` plus a unique `check_ins.ticket_id` |
| QR codes can't be forged | Random UUID ticket IDs; HMAC-SHA256 signature checked at check-in, and must match the stored signature |
| Every issued order reaches notification-service | Outbox row committed with the tickets; published with confirms and retried until the broker has it |
| Email failure never undoes a booking | The email is notification-service's; tickets are committed before `TicketIssued` is even published |

## Tests

```sh
ruff check . && ruff format --check .
python -m pylint app tests alembic
pytest                          # unit tests (SQLite; no services needed)
docker compose run --rm tests   # integration tests (MySQL, RabbitMQ, API, worker)
```

### Contract tests

`tests/contract` checks Ticket against the AMQP message contracts owned by rabbitmq-service.
The tests are generated by rabbitmq-service's `ticketto-contracts` package, from its catalogue:

- **Publishing** (`TicketIssued`): the exchange, routing key, `mandatory`, publisher confirms,
  the AMQP properties, the envelope and the exchange declare. `tests/contract/conftest.py`
  applies an `EventPublished` and an `OrderConfirmed` through the real handler, then runs the
  real relay (`outbox.run_once` with `RabbitPublisher`) with only its broker connection
  replaced. The payload itself is not checked yet: the catalogue has no `TicketIssued` schema.
- **Consuming** (`EventPublished`, `EventCancelled`, `OrderConfirmed`, `RefundCompleted`): the
  worker consumes exactly Ticket's queues. The real consumer path (`dispose` plus
  `handle_message`) acks a body with only the publisher's required fields, ignores unknown
  fields, and dead-letters a body missing a required field.

No broker or database server is needed: they use the unit tests' SQLite database.

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
here (on `main`) for every contract merge request that affects ticket-service, with
`CONTRACTS_VERSION` set to that merge request's pre-release. Those pipelines run only
`contract-tests`, never build or deploy, and a failure blocks the contract change.

## Deployment

The GitLab pipeline tests every merge request and deploys `main` to ECS as two services from one image: the API (`ticket-service`) and the worker (`ticket-service-worker`, `python -m app.worker`). Deploy jobs run once the CI/CD variable `DEPLOY_ENABLED` is `true`. To roll back, run the manual `rollback` job on `main`, or re-run the `deploy` job of an earlier pipeline. AWS setup: [`deploy/README.md`](deploy/README.md).
