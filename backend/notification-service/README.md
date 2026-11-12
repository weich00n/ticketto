# Notification Service

Emails Ticketto's clients when something happens to an event they care about. A **worker** on ECS Fargate keeps taking its messages off the shared RabbitMQ broker, works out who to email, and sends the emails through Amazon SES.

| Message (publisher) | Who gets an email |
| --- | --- |
| `EventPublished` (event-service) | Every user whose new-event emails are on (`notify_new_events`, on by default; users turn it off with `PATCH /api/v1/users/me`) |
| `EventCancelled` (event-service) | Only users who bought tickets for that event |
| `OrderRefunded` (checkout-service) | Only that order's buyer |
| `RefundFailed` (checkout-service) | Only that order's buyer: the refund didn't go through, so reply to the email |
| `TicketIssued` (ticket-service) | Only that order's buyer: their tickets' details and QR codes |

Python 3.12, SQLAlchemy, Alembic, MySQL, pika and the SES API.

It also serves the **notifications API** for the frontend's bell: each signed-in user's notifications, through the shared API Gateway (see [below](#notifications-api)).

**Message contract:** [docs/MESSAGES.md](docs/MESSAGES.md): the fields each message needs, how each is acked or dead-lettered, and the user-service calls behind it.

## How it works

```
ECS service notification-service-worker (python -m app.worker), run after run:
  1. basic_get from notification-service.EventPublished / .EventCancelled / .OrderRefunded
     / .RefundFailed / .TicketIssued (rabibi:5672)
  2. ask user-service who to email (internal API)
  3. queue one row per email in MySQL (db_notification)
  4. send due emails through SES, with retries
```

- **Who to email comes from user-service.** It stores the new-event setting and, from `OrderConfirmed`, each user's purchased events (see [user-service's README](../user-service/README.md#messages-the-worker)). Endpoints:
  - `GET /internal/v1/users?notify_new_events=true`
  - `GET /internal/v1/users?purchased_event_id=<id>`
  - `GET /internal/v1/users/by-sub/{sub}`. checkout-service's `user_id` is the Cognito sub.
- **It runs in short runs.** Each run lasts up to 40 s (`WORKER_RUN_SECONDS`). The next starts after 10 s (`WORKER_POLL_SECONDS`), at once if it hit its limit, or after 60 s if it failed (`WORKER_RETRY_SECONDS`). Each run pulls messages with `basic_get`, following the broker's rules ([MESSAGING.md](../rabbitmq-service/docs/MESSAGING.md)):
  - **Handled:** ack only after the database commit.
  - **Poison:** `basic_reject(requeue=False)`, so it goes straight to the DLQ.
  - **Transient failure** (user-service or the database is down): `basic_reject(requeue=True)`, then that queue waits for the next run. Each failed run uses one of the message's 5 deliveries; after about 5 minutes of outage it goes to the DLQ.
  - Never nack, never auto-ack, never declare a queue.
- **Half of each run takes messages in, the rest sends** (`DRAIN_BUDGET_FRACTION`). A backlog can't stop emails going out, and unread messages wait safely on the queue.
- **Emails go out with retries.** Each email is a `notifications` row:
  - **Claim:** a run takes the row with `FOR UPDATE SKIP LOCKED`, which also leases it for 10 minutes.
  - **Send:** if the run dies mid-send, the lease expires and the email is retried.
  - **Fail:** a failed send retries after 30 s, 60 s, 120 s and so on. After 5 tries it's `FAILED`.
- **No duplicates.**
  - Every message is processed once (`processed_messages`).
  - Every email has a unique `dedupe_key` (`<kind>:<event>:<user>`, or the refund ID), so a publisher re-sending the same fact under a new `message_id` doesn't send a second email.
- **A cancellation always wins.** It arrives on a separate queue and can be handled before the `EventPublished`. When it is:
  - The event's row in `events` records the cancellation, so no new-event email goes out.
  - New-event emails still queued are cancelled.
- **QR codes.** A `TicketIssued` message's `qr_payload` works like the ticket itself. Each one stays in the email's row only until the email reaches a final status (`SENT`, `FAILED` or `CANCELLED`). It's never logged or returned by the notifications API, and buyers see the code in the app on `/tickets`.
- **Event titles.** `events` keeps the title from `EventPublished`, because `OrderRefunded` and `RefundFailed` have none. For an event published before this service existed, it asks event-service (`EVENT_SERVICE_URL`, optional), or the email says "your event".

## Notifications API

Every email this service queues is also an in-app notification. The ECS service `notification-service` (same image, `uvicorn app.main:app` on port 8082, code in [`app/api.py`](app/api.py)) serves them on the shared HTTP API, through the shared VPC link and internal ALB. Every route requires a Cognito token:

| Route | Returns |
| --- | --- |
| `GET /api/v1/notifications?page=1&page_size=20` | The caller's notifications, newest first: `{items, page, page_size, total_items}` (`page_size` 1–100) |
| `GET /api/v1/notifications/unread-count` | `{"unread": 3}` |
| `POST /api/v1/notifications/{notification_id}/read` | `204`; `404` if it isn't the caller's |
| `POST /api/v1/notifications/read-all` | `204` |

Each item:
```json
{"notification_id": 10, "kind": "ORDER_REFUNDED",
 "title": "Your refund of SGD 88.00 for Autumn Live",
 "preview": "We've refunded SGD 88.00 for Autumn Live because the event was cancelled.",
 "event_id": "…", "order_id": "…", "created_at": "2026-10-28T12:50:56Z",
 "email_status": "SENT", "read_at": null}
```

- **Who's asking:** the `sub` claim of the token that API Gateway verified (payload format 2.0). Each notification row stores its recipient's `cognito_sub`, which user-service's internal lists return. Rows queued before migration `0002` have none, so they never show.
- **What shows:** `PENDING`, `SENT` and `FAILED` emails. A failed email still shows in the app. Withdrawn (`CANCELLED`) new-event emails don't.
- **Read** means seen in the app: the frontend calls `read-all` when the bell opens, or `{id}/read` for one. Email opens aren't tracked.
- **Access:**
  - The function has no function URL; API Gateway may invoke it only for `/api/v1/notifications*`.
  - Without a valid token, the gateway answers `401` itself.
  - Errors are `{"error": {"code", "message"}, "correlation_id"}`.

## Running locally

Start the shared broker first; it has the topology and this service's broker user:

```sh
(cd ../rabbitmq-service && docker compose up -d --build)
docker compose up -d --build        # the worker, the API, MySQL (3312), Mailpit and a user-service stub
open http://localhost:8026          # every email lands in Mailpit
```

The `notification-worker` container is the worker, and `notification-api` is the API on http://localhost:18080. Publish a message in the broker UI (http://localhost:15672, `ticketto` / `ticketto-local`), for example exchange `event.events`, routing key `event.published`, with a body from [`tests/fixtures`](tests/fixtures).

- **With the real user-service** instead of the stub, run its stack with the worker (`docker compose --profile worker up -d` there), then here: `USER_SERVICE_URL=http://host.docker.internal:8000 docker compose up -d notification-worker`.

## Tests

```sh
python3.12 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'
ruff check . && ruff format --check . && python -m pylint app tests alembic scripts
pytest                          # unit tests: SQLite, fake broker channel, fakes for user-service and SES
docker compose run --rm tests   # integration tests: the stack above, the real broker, Mailpit
```

| Suite | Checks |
| --- | --- |
| Unit (`tests/unit`) | <ul><li>**Parsing:** the publishers' example messages, and poison messages.</li><li>**Recipients:** opted-in users for new events, buyers for cancellations, the one buyer for refunds.</li><li>**No duplicates:** the same message twice, or the same fact under a new `message_id`, sends one email.</li><li>**Cancellation wins** in either order.</li><li>**Retries:** 30 / 60 / 120 s backoff, leases, and overlapping runs.</li><li>**The broker:** `basic_get` / ack / reject only, a failing queue stops for the run, the time budget.</li><li>**Email content and the worker:** the email text, the worker loop, config, and migrations up and down.</li></ul> |
| Integration (`tests/integration`) | Messages published to the real broker reach only the right people in Mailpit. Poison goes to the DLQ, and a duplicate sends one email |

### Contract tests

`tests/contract` checks notification-service against the AMQP message contracts owned by
rabbitmq-service. The tests are generated by rabbitmq-service's `ticketto-contracts` package,
from its catalogue. notification-service only consumes, so they check that:

- each run drains exactly its queues (`app.broker.QUEUES`)
- the real path (`dispose` plus `handle_message`, on SQLite with the user-service and
  event-service fakes) acks each of `EventPublished`, `EventCancelled`, `OrderRefunded`,
  `RefundFailed` and `TicketIssued` when it carries only the fields its publisher always sends,
  ignores fields it doesn't know, and dead-letters it when a required field it reads is missing

The fields notification-service reads are listed in rabbitmq-service's
`contracts/requirements/notification-service.yaml`.

The package is pinned in `requirements-contract.txt`. It comes from rabbitmq-service's GitLab
package registry; if the registry is private, you need a GitLab personal access token
(`read_api`):

```sh
export CONTRACTS_INDEX_USER=__token__ CONTRACTS_INDEX_TOKEN=<your token>
pip install -e '.[dev,contract]'
pip install --no-deps -r requirements-contract.txt
pytest tests/contract -m contract
```

Without the package they are skipped, so `pytest` and the other jobs don't need it. To move to a
new contract version, change `ticketto-contracts==X.Y.Z` in `requirements-contract.txt`.
rabbitmq-service's `contracts/CHANGELOG.md` says what changed.

In CI, `contract-tests` runs them on every pipeline. rabbitmq-service also triggers a pipeline
here (on `main`) for every contract merge request that affects notification-service, with
`CONTRACTS_VERSION` set to that merge request's pre-release. Those pipelines run only
`contract-tests`, never build or deploy. A failure blocks a patch or minor contract change, and
is a warning for a major one.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `DATABASE_URL` | | MySQL URL. When unset, it's built from `DB_HOST`, `DB_PORT`, `DB_NAME` (`db_notification`), `DB_USER` and `DB_PASSWORD` |
| `DB_SSL_CA` | | RDS CA bundle (`/etc/ssl/rds/global-bundle.pem` in the image). Turns on TLS |
| `RABBITMQ_URL` | required | As `notification-service`, e.g. `amqp://notification-service:<password>@<broker>:5672/%2F` |
| `USER_SERVICE_URL` | required | user-service's private address, e.g. `http://user-service.ticketto.local:8001` (its internal port) |
| `EVENT_SERVICE_URL` | | Optional: titles for events published before this service started |
| `EMAIL_BACKEND` | `ses` | `ses` (the SES API, with the worker's task role), `smtp` (Mailpit), or `log` |
| `EMAIL_FROM` | `Ticketto <notifications@ticketto.local>` | Must be an SES-verified address or domain in AWS |
| `SES_CONFIGURATION_SET` | | Makes SES publish each email's delivery events (`ses` only). Set in AWS |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_STARTTLS` | `localhost`, `1025` | For `EMAIL_BACKEND=smtp` |
| `EMAIL_MAX_ATTEMPTS`, `EMAIL_RETRY_BASE_SECONDS` | `5`, `30` | Retries: 30 s, 60 s, 120 s... |
| `DRAIN_BUDGET_FRACTION` | `0.5` | Share of each run spent taking messages in |
| `LOG_LEVEL` | `INFO` | One JSON object per line |

## CI/CD

| Stage | Jobs |
| --- | --- |
| lint | ruff, pylint, shellcheck, `tofu-validate` |
| test | Unit; integration; SAST; secret detection. Integration uses MySQL, a stock RabbitMQ and Mailpit as GitLab services: [`declare_test_topology.py`](scripts/ci/declare_test_topology.py) gives the broker this service's queues, with the same arguments as rabbitmq-service's topology |
| build | MRs: the image builds. `main`: push `:<sha>` and `:latest` to ECR |
| deploy (`main`) | <ol><li>[`ecs-migrate.sh`](scripts/ci/ecs-migrate.sh): register both task definitions for the new image, then run `alembic upgrade head` as a one-off ECS task, and fail if it errors</li><li>[`ecs-deploy.sh`](scripts/ci/ecs-deploy.sh): roll the API and worker onto them and wait until healthy</li></ol>Rollback: re-run an earlier pipeline's `deploy` |

The build-and-push and deploy jobs run once the `DEPLOY_ENABLED` CI/CD variable is `true`.

## Deploying to AWS

Everything is in [`deploy/terraform`](deploy/terraform), in the account's default VPC:
- two Fargate services in the shared `TicketPlatform` cluster, 0.25 vCPU and 512 MB each ([`ecs.tf`](deploy/terraform/ecs.tf)): the worker, and the API with its four routes on the shared HTTP API ([`api.tf`](deploy/terraform/api.tf)). The gateway stack (`frontend/deploy/gateway`) owns the API's ALB listener and target group
- an SES configuration set that records what happens to each email ([`email_tracking.tf`](deploy/terraform/email_tracking.tf))
- security group rules into user-service (8000) and RDS (3306)
- the database secret
- ECR, and the GitLab OIDC deploy role

The tasks read the broker login and the database password from Secrets Manager, by ARN. The VPC has no NAT, so they have public IPs to reach AWS's APIs, SES included; their security groups let nothing in except the ALB to the API.

### First deploy (in order)

1. **Broker (rabbitmq-service).** Merge its topology change, which adds this service's queues and user. `tofu apply` in its `deploy/terraform` **before** the merge: it creates the secret `rabibi/service/notification-service`.
2. **SES** (console, us-east-1):
   1. Verify the sender (SES → Identities → Create identity: an address you own, or a domain).
   2. The account is in the **SES sandbox**: emails only reach verified addresses, at 200 a day. Verify the team's test addresses too, or request production access (SES → Account dashboard).

   No SMTP credentials are needed: the worker calls the SES API with its task role.
3. **Infrastructure, with no tasks yet.** The image doesn't exist, so both services start at 0 tasks. The gateway stack needs the API's security group, and this stack needs the gateway's target group:
   ```sh
   cd deploy/terraform
   cp backend.hcl.example backend.hcl            # fill in the account ID
   cp terraform.tfvars.example terraform.tfvars  # set ses_sender
   tofu init -backend-config=backend.hcl
   tofu apply -target=aws_security_group.api
   ```
   Apply `frontend/deploy/gateway`, then run `tofu apply` here.
4. **Broker access.** Add the output `worker_security_group_id` to `amqp_allowed_sg_ids` in rabbitmq-service's `terraform.tfvars`, then `tofu apply` there.
5. **Database.** With `db-ticketing` running (tell the team before starting it), run `./deploy/create-database.sh`. It creates `db_notification` and its user from the `notification-service/db` secret.
6. **GitLab.**
   1. Push this repo to `cs302-2026/g2-team5/backend/notification-service`.
   2. Add every value from `tofu output -json gitlab_ci_variables` as a CI/CD variable (Protected, environment scope `production`).
   3. Add `DEPLOY_ENABLED` = `true` (Protected, scope All).
   4. Run the `main` pipeline. It pushes the image, runs the migration and updates both services, still at 0 tasks.
7. **The tasks.** Set `desired_count = 1` and `worker_desired_count = 1` in `terraform.tfvars`, then `tofu apply`. From then on every merge to `main` deploys.
8. **user-service** must be running its worker (`user-service-worker`), or buyers aren't recorded: its [deploy/README.md](../user-service/deploy/README.md#the-worker-rabbitmq-consumer).

### Day to day

| Task | How |
| --- | --- |
| See what each run did | CloudWatch `/ecs/notification-service`, streams `notification-service-worker/...`: one `run finished` line per busy run, with counts per queue and emails sent |
| See API requests | CloudWatch `/ecs/notification-service`, streams `notification-service/...`: one `request completed` line per request, with the path, status and the caller's `user_sub` |
| Did an email arrive? | SES accepting an email only starts delivery. Take `ses_message_id` from its `email sent` line in `/ecs/notification-service`, then search `/aws/events/notification-service-emails` for it. `Delivery` means Gmail or the other server accepted it; `DeliveryDelay` means SES is still retrying; `Bounce`, `Complaint` and `Reject` are final. Those events include the recipient's address, so they're kept for 14 days |
| Failed emails | In `db_notification`: `SELECT id, kind, recipient, attempts, last_error FROM notifications WHERE status = 'FAILED'`. To retry one: `UPDATE notifications SET status = 'PENDING', attempts = 0, next_attempt_at = UTC_TIMESTAMP() WHERE id = ...` |
| Failed messages | The broker UI: `notification-service.<Message>.dlq`. The `x-death` header says why. Fix the cause, then **Move messages** back to the queue ([rabbitmq-service deploy/README.md](../rabbitmq-service/deploy/README.md#4-day-to-day)) |
| Pause | Set `worker_desired_count = 0`, then `tofu apply`. Messages wait on their queues |
