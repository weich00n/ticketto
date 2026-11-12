# Event Service

Manages events for Ticketto: event details, ticket categories, prices, quantities, release time, publication and cancellation. Live availability, holds and the purchase limit belong to Reservation Service.

Python 3.12, Flask, SQLAlchemy, Alembic, MySQL and RabbitMQ.

## How it fits

| Who | How they use it |
| --- | --- |
| Buyers and organisers | Through API Gateway: `/api/v1/events` and `/api/v1/organizer/events` |
| Reservation and Checkout | Directly, to read a category's price and quantity (`/internal/v1`) |
| Queue, Reservation, Checkout and Ticket | They consume the `EventPublished` (not Checkout) and `EventCancelled` messages this service sends (see [Messages](#messages)) |

It runs as two processes from one image: the **API** and the **worker**. The worker marks events `COMPLETED` once they start and sends the pending messages from the outbox table.

## Running locally

```sh
docker compose up --build
```

This runs the migrations and starts:

- the API on `http://localhost:3001`
- the worker, which sends outbox messages and marks events `COMPLETED` once they start
- MySQL on port `3307` and RabbitMQ on `5672` (UI at `http://localhost:15672`)

That RabbitMQ is a plain broker for this stack only. To see messages reach the other services' queues, run the shared broker from rabbitmq-service instead (`docker compose up -d --build` there) and point the API and worker at it from your machine:

```sh
export RABBITMQ_URL='amqp://ticketto:ticketto-local@127.0.0.1:5672/%2F'  # local-only password; gitleaks:allow
```

Use `127.0.0.1`, not `localhost`: the shared broker listens on IPv4 only. Its UI (user `ticketto`) shows each consumer's queue, such as `reservation-service.EventPublished`.

To run the API outside Docker:

```sh
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
export DATABASE_URL='mysql+pymysql://ticketto:ticketto@localhost:3307/event_service'
alembic upgrade head
flask --app 'app.main:create_app()' run --port 3001
```

## Configuration

| Variable | Description |
| --- | --- |
| `DATABASE_URL` | MySQL URL. Used locally; when unset, the URL is built from `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER` and `DB_PASSWORD` |
| `DB_SSL_CA` | Path to the RDS CA bundle (`/etc/ssl/rds/global-bundle.pem` in the image). Enables TLS |
| `RABBITMQ_URL` | RabbitMQ URL |
| `PORT` | API port, default `3001` |
| `LOG_LEVEL` | Default `INFO` |

## Project layout

```text
app/
  main.py          App factory: error handlers and blueprints
  routes/          One module per route group: health, public, organizer, internal
  schemas.py       Request validation (Pydantic)
  serializers.py   Response shapes
  models.py        Tables: events, ticket_categories, outbox_messages
  status.py        Event statuses
  outbox.py        Builds outbox messages and sends them to RabbitMQ
  lifecycle.py     Marks started events COMPLETED
  worker.py        Loop that runs lifecycle.py and outbox.py
  auth.py          Reads the identity headers set by API Gateway
  config.py, db.py, errors.py, correlation.py, logging_config.py
alembic/           Database migrations
deploy/            ECS task definitions and AWS setup guide
scripts/ci/        Pipeline scripts: migrate, deploy and integration tests
tests/             unit/ (SQLite) and integration/ (MySQL and RabbitMQ)
```

## Authentication

API Gateway validates the Cognito token and forwards `X-User-Sub`, `X-User-Username` and `X-User-Groups`. Organiser routes need these headers and the `organiser` group. `X-User-Sub` is stored as `organiser_id`, and organisers can only see and change their own events (others return `404`).

The service must only be reachable through API Gateway and from other services. `/internal/v1` has no gateway route.

## API

| Method | Route | Access | Description |
| --- | --- | --- | --- |
| `GET` | `/health` | Public | Liveness |
| `GET` | `/ready` | Public | Database readiness |
| `GET` | `/api/v1/events?search=&page=&page_size=` | Public | Published events, soonest first |
| `GET` | `/api/v1/events/{event_id}` | Public | Published, cancelled or completed event |
| `POST` | `/api/v1/organizer/events` | Organiser | Create a draft |
| `GET` | `/api/v1/organizer/events?status=&page=&page_size=` | Organiser | Own events, newest first |
| `GET` | `/api/v1/organizer/events/{event_id}` | Owner | Own event in any status |
| `PUT` | `/api/v1/organizer/events/{event_id}` | Owner | Replace a draft |
| `POST` | `/api/v1/organizer/events/{event_id}/publish` | Owner | Publish a draft |
| `POST` | `/api/v1/organizer/events/{event_id}/cancel` | Owner | Cancel a published event (`{"reason": "..."}`) |
| `GET` | `/api/v1/events/{event_id}/image` | Public | The event's image (published, cancelled or completed events) |
| `PUT` | `/api/v1/organizer/events/{event_id}/image` | Owner | Upload or replace the image of a draft or published event |
| `GET` | `/api/v1/organizer/events/{event_id}/image` | Owner | The image in any status, to preview a draft |
| `GET` | `/internal/v1/events/{event_id}/categories/{category_id}` | Services | Category price and quantity |

Fields are snake_case, times are UTC (`2026-10-20T10:00:00Z`), and money is an integer in cents (`8800` = SGD 88.00).

Create or update a draft:

```json
{
  "title": "The Midnight Symphony",
  "description": "An evening of orchestral music.",
  "venue": "Victoria Theatre & Concert Hall",
  "category": "Concerts",
  "starts_at": "2026-11-14T19:30:00Z",
  "ticket_release_at": "2026-10-20T10:00:00Z",
  "ticket_categories": [
    { "name": "CAT 1", "price_amount": 8800, "currency": "SGD", "total_quantity": 500 }
  ]
}
```

Rules:

- Both times must be in the future, with release before start.
- `category` is optional: `Concerts`, `Theatre`, `Festivals`, `Sports` or `Conferences`, or `null`. It is returned on every event response, and it is not part of the `EventPublished` message.
- 1–10 categories with unique names. `price_amount` ≥ 0, `total_quantity` ≥ 1.
- `PUT` replaces the whole draft and gives its categories new IDs.
- Only drafts can be updated or published, and only published events cancelled (`409 INVALID_EVENT_STATE` otherwise). See [Event lifecycle](#event-lifecycle).
- Publishing needs the release at least 15 minutes away, since buyers can join the queue 15 minutes before (`queue_join_opens_at`).

Event images:

- Send the file itself as the request body of `PUT .../image`, with its type as the `Content-Type` (`image/jpeg`, `image/png` or `image/webp`). The file's first bytes must match that type.
- The limit is 300 KB (`413 IMAGE_TOO_LARGE`); other or mismatched types get `415 UNSUPPORTED_IMAGE_TYPE`. Cancelled and completed events can't change their image (`409 INVALID_EVENT_STATE`).
- Events carry `image_url` (a path such as `/api/v1/events/{event_id}/image?v=2`, or `null` without an image). The number changes with every upload, so the public response can be cached for a year.
- Images are stored in the `event_images` table and don't appear in any message.

Organiser responses also include `organiser_id`, `created_at`, `updated_at`, `published_at`, `cancelled_at` and `cancellation_reason`. Lists return `items`, `page`, `page_size` and `total_items`.

Errors:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "The request is invalid.",
    "details": { "starts_at": "Must be in the future." }
  },
  "correlation_id": "4b0c…"
}
```

Codes: `VALIDATION_ERROR` (400), `UNAUTHENTICATED` (401), `FORBIDDEN` (403), `EVENT_NOT_FOUND` (404), `INVALID_EVENT_STATE` (409), `SERVICE_UNAVAILABLE` (503).

Requests can send `X-Correlation-Id`; it is returned in the response, logged, and copied onto any message the request creates.

## Event lifecycle

```text
DRAFT ──publish──▶ PUBLISHED ──cancel──▶ CANCELLED
                       │
                       └─ start time passes (worker) ─▶ COMPLETED
```

| Status | Who sees it | Allowed changes |
| --- | --- | --- |
| `DRAFT` | Its organiser only | Update (replaces the draft), publish |
| `PUBLISHED` | Everyone, in the event list | Cancel |
| `CANCELLED` | Everyone, by event ID | None |
| `COMPLETED` | Everyone, by event ID | None |

Every change raises `event_version`, so consumers can ignore anything older than what they have.

## Messages

Publish and cancel save the change and its message in one transaction. The worker sends pending messages in order to the `event.events` topic exchange. Delivery is at least once: consumers should deduplicate by `message_id` and ignore an `event_version` they have already passed.

The shared broker's catalogue in rabbitmq-service (`topology/messages.yaml`, payload schemas in `contracts/`) is the source of truth for exchanges, routing keys and consumers. Change it there first.

| Message | Routing key | Consumers (broker queues) |
| --- | --- | --- |
| `EventPublished` | `event.published` | `queue-service.EventPublished`, `reservation-service.EventPublished`, `ticket-service.EventPublished` |
| `EventCancelled` | `event.cancelled` | `queue-service.EventCancelled`, `reservation-service.EventCancelled`, `checkout-service.EventCancelled`, `ticket-service.EventCancelled` |

Each publish is persistent, uses publisher confirms and `mandatory`, and sets the AMQP `message_id`, `type` (the message name), `correlation_id` (the request's `X-Correlation-Id`) and `content_type`.

Marking an event `COMPLETED` once it starts raises its `event_version` but sends no message: consumers already have `starts_at`.

Envelope:

```json
{
  "message_id": "5d3c2b1a-7e8f-4a90-b1c2-d3e4f5a6b7c8",
  "message_type": "EventPublished",
  "schema_version": 1,
  "occurred_at": "2026-10-14T08:00:00Z",
  "producer": "event-service",
  "payload": {}
}
```

`EventPublished` payload:

```json
{
  "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
  "event_version": 2,
  "title": "The Midnight Symphony",
  "venue": "Victoria Concert Hall",
  "starts_at": "2026-11-21T19:30:00Z",
  "release_at": "2026-11-20T12:00:00Z",
  "queue_join_opens_at": "2026-11-20T11:45:00Z",
  "published_at": "2026-10-14T08:00:00Z",
  "total_capacity": 300,
  "ticket_categories": [
    { "category_id": "…", "name": "CAT 1", "price_amount": 8800, "currency": "SGD", "total_quantity": 300 }
  ]
}
```

`EventCancelled` payload:

```json
{
  "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
  "event_version": 3,
  "cancellation_id": "0fd4a84a-f4af-4a25-8ead-00430618aaaa",
  "cancelled_at": "2026-11-19T09:30:00Z",
  "reason": "Venue closure"
}
```

`venue` is not in the broker's `EventPublished` schema yet; consumers ignore fields they don't know, so it is safe to send before the schema is updated.

`tests/unit/test_messages.py` checks this format, so update it together with this section.

## Tests

```sh
ruff check . && ruff format --check .
python -m pylint app tests alembic
pytest                          # unit tests
docker compose run --rm tests   # integration tests (MySQL, RabbitMQ)
```

### Contract tests

`tests/contract` checks that what the outbox relay publishes matches the AMQP message
contracts owned by rabbitmq-service: for `EventPublished` and `EventCancelled`, the exchange,
routing key, `mandatory`, publisher confirms, the AMQP properties, the envelope and the payload
against its schema. The tests are generated by rabbitmq-service's `ticketto-contracts` package.
`tests/contract/conftest.py` publishes and cancels an event through the API and runs the real
relay, `publish_pending_messages`, with only its broker connection replaced. No broker or
database server needed.

The package is pinned in `requirements-contract.txt` and comes from rabbitmq-service's GitLab
package registry, with a GitLab personal access token (`read_api`) if the registry is private:

```sh
export CONTRACTS_INDEX_USER=__token__ CONTRACTS_INDEX_TOKEN=<your token>
pip install '.[dev,contract]'
pip install --no-deps -r requirements-contract.txt
pytest tests/contract -m contract
```

Without the package they are skipped, so `pytest` and the other jobs don't need it. To move to
a new contract version, change `ticketto-contracts==X.Y.Z` in `requirements-contract.txt` (what
changed: rabbitmq-service's `contracts/CHANGELOG.md`).

In CI, `contract-tests` runs them on every pipeline. rabbitmq-service also triggers a pipeline
here (on `main`) for every contract merge request that affects event-service, with
`CONTRACTS_VERSION` set to that merge request's pre-release: those pipelines run only
`contract-tests`, never build or deploy, and a failure blocks the contract change.

## Deployment

The GitLab pipeline tests every merge request, and on `main` builds the image and pushes it to ECR. Deploying it to ECS is a manual button: press **`migrate`** on the `main` pipeline, and `deploy` follows once it succeeds (the pipeline shows "blocked" until then). Deploy jobs only exist once the CI/CD variable `DEPLOY_ENABLED` is `true`; until then `main` runs the checks and builds the image. AWS setup steps are in [`deploy/README.md`](deploy/README.md).
