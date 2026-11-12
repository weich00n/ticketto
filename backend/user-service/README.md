# User Service

Ticketto's User Service. It stores user profiles (`user_id`, Cognito `sub`, name, email, role) and upgrades buyers to organisers. It is built with FastAPI, SQLAlchemy and MySQL.

- Design and decisions: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)
- What was built and why: [IMPLEMENTATION_NOTES.md](IMPLEMENTATION_NOTES.md)

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/users/me` | Get the caller's profile |
| POST | `/api/v1/users/me` | Create the caller's profile on first login (idempotent) |
| PATCH | `/api/v1/users/me` | Update the caller's `name` and/or `notify_new_events` (new-event emails, on by default) |
| POST | `/api/v1/users/me/organiser` | Upgrade the caller to organiser |
| GET | `/internal/v1/users/{user_id}` | Service-to-service profile lookup (no API Gateway route) |
| GET | `/internal/v1/users/by-sub/{sub}` | Profile by Cognito sub, the `user_id` in checkout-service's messages (internal) |
| GET | `/internal/v1/users?notify_new_events=true` or `?purchased_event_id=<uuid>` | Users to email, paged with `after` / `limit` (default 500, max 1000); returns `items` (`user_id`, `cognito_sub`, `email`, `name`) and `next_after` (internal, used by notification-service) |
| GET | `/health` | Liveness |
| GET | `/health/ready` | Readiness (checks MySQL) |

`/api/v1` routes identify the caller from the `X-User-Sub` and `X-User-Username` headers, which API Gateway sets after verifying the access token. Interactive API docs are at `http://localhost:8000/docs` while the service is running.

## Run locally (Docker Compose)

Requires Docker and the AWS CLI.

```bash
cp .env.example .env
docker compose up -d mysql cognito-local
./scripts/seed-cognito-local.sh          # creates the pool and writes COGNITO_USER_POOL_ID into .env
docker compose up -d --build user-service
curl localhost:8000/health
```

Compose runs `alembic upgrade head` before starting the service. MySQL is exposed on host port **3307**.

There is no API Gateway locally, so set the identity headers yourself. With email sign-in, Cognito's `username` is the same value as `sub`. You can read it from a token or from `aws cognito-idp admin-get-user`:

```bash
SUB=<the user's sub>
curl -X POST localhost:8000/api/v1/users/me -H "X-User-Sub: $SUB" -H "X-User-Username: $SUB"
```

**If you get "Cognito pool ... not found"** (from the tests or the service): cognito-local's data lives in `.cognito/`, and the pool ID is random each time a pool is created. If that folder is deleted or recreated, `.env` points at a pool that no longer exists. Fix it by re-running `./scripts/seed-cognito-local.sh` (it updates `.env`), then `docker compose up -d user-service` so the service picks up the new ID.

Stop everything with `docker compose down`. Add `-v` to also delete the MySQL data.

## Messages (the worker)

`python -m app.worker` (Compose: `docker compose --profile worker up -d`, with `../rabbitmq-service`'s broker running) consumes two queues from the shared broker, following rabbitmq-service's [docs/MESSAGING.md](../rabbitmq-service/docs/MESSAGING.md): manual ack after commit, `basic_reject` (never nack), and queues consumed by name, never declared.

| Queue | Effect |
|---|---|
| `user-service.OrderConfirmed` | Adds `event_id` to the buyer's `purchased_event_ids` (comma-separated; the buyer is matched on `cognito_sub`, because checkout-service's `user_id` is the Cognito sub). Skipped if the event is already cancelled. An unknown sub is retried and dead-lettered after 5 redeliveries |
| `user-service.EventCancelled` | Records the event in `cancelled_events`. Buyers keep it in their list, so notification-service can still find who to email |

Both are idempotent by effect, so redeliveries are harmless.

## Run tests

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

There are three suites, plus the contract tests (below):

| Suite | Command | Needs | Checks |
|---|---|---|---|
| Unit (`tests/unit/`) | `pytest tests/unit` | Nothing (in-memory SQLite, fake Cognito) | The code's logic, including failure paths such as Cognito outages |
| Integration (`tests/integration/`) | `pytest -m integration` | The Compose stack running and seeded | The real service with real MySQL and cognito-local: schema, data saved, Cognito updates, real tokens, concurrent first logins |
| Worker end to end (`tests/integration/test_worker.py`) | `pytest -m broker` | The stack with the worker (`docker compose --profile worker up -d`) and `../rabbitmq-service`'s broker | OrderConfirmed and EventCancelled through the real broker: purchases recorded, a late order for a cancelled event ignored, DLQs empty |

A plain `pytest` also runs only the unit tests (`pytest.ini` deselects the integration ones).

**To run both suites together**, pass an empty marker expression, which replaces the default filter and selects everything (77 tests). The integration tests need the stack running:

```bash
python -m pytest -m ""
docker compose run --rm tests pytest -m ""     # same, in Docker
```

### In Docker (no local Python needed)

```bash
docker compose run --rm tests                                   # integration suite
docker compose run --rm tests pytest -m integration -k organiser  # a subset
docker compose run --rm --no-deps tests pytest                  # unit tests only (no stack needed)
```

- **What it does:** the `tests` service starts `mysql`, `cognito-local` and `user-service` if they aren't running, waits until the service is healthy, then runs pytest on the Compose network.
- **Prerequisite:** `.env` must contain the pool ID from `./scripts/seed-cognito-local.sh` (first time only).
- **No rebuild needed:** your source is mounted into the container, so code and test changes are picked up immediately. Add `--build` only after changing `requirements*.txt`, or after changing service code, since `user-service` itself runs from its built image.
- **Afterwards:** the stack is left running. Stop it with `docker compose down`.

### On your machine

```bash
source .venv/bin/activate             # see "Run tests" above for creating it
docker compose up -d --build          # stack running, with COGNITO_USER_POOL_ID set in .env
python -m pytest -m integration
```

- **Configuration:** it reads the pool ID and DB credentials from `.env`. Override any value with an `IT_`-prefixed env var, e.g. `IT_SERVICE_URL=http://localhost:8001`.
- **Test users:** each test creates its own Cognito user (`it-<random>@test.com`) and deletes the user and their MySQL row afterwards, so your seed users and data are left alone.
- **Stack down:** if the stack isn't running, the tests fail with a message saying what to start. They don't skip silently.

### Contract tests

`tests/contract` checks user-service against the AMQP message contracts owned by
rabbitmq-service. The tests are generated by rabbitmq-service's `ticketto-contracts` package,
from its catalogue. user-service only consumes, so they check that:

- the worker consumes exactly its queues (`user-service.OrderConfirmed`,
  `user-service.EventCancelled`)
- the real consumer path (`dispose` plus `handle_message`, on an in-memory SQLite database) acks
  an `OrderConfirmed` and an `EventCancelled` that carry only the fields their publishers always
  send, and ignores fields it doesn't know

They don't send bodies with a required field missing: for `OrderConfirmed` that drops `order_id`,
which user-service doesn't read, so acking it is correct. The fields user-service reads are listed
in rabbitmq-service's `contracts/requirements/user-service.yaml`.

The package is pinned in `requirements-contract.txt`. It comes from rabbitmq-service's GitLab
package registry; if the registry is private, you need a GitLab personal access token
(`read_api`):

```bash
export CONTRACTS_INDEX_USER=__token__ CONTRACTS_INDEX_TOKEN=<your token>
pip install -r requirements-dev.txt
pip install --no-deps -r requirements-contract.txt
pytest tests/contract -m contract
```

Without the package they are skipped, so `pytest` and the other jobs don't need it. To move to a
new contract version, change `ticketto-contracts==X.Y.Z` in `requirements-contract.txt`.
rabbitmq-service's `contracts/CHANGELOG.md` says what changed.

In CI, `contract-tests` runs them on every pipeline. rabbitmq-service also triggers a pipeline
here (on `main`) for every contract merge request that affects user-service, with
`CONTRACTS_VERSION` set to that merge request's pre-release. Those pipelines run only
`contract-tests`, never build or deploy. A failure blocks a patch or minor contract change, and
is a warning for a major one.

## Configuration

| Variable | Local | AWS |
|---|---|---|
| `AWS_REGION` | `us-east-1` | `us-east-1` |
| `COGNITO_ENDPOINT` | `http://cognito-local:9229` (set by Compose) | *(unset)* |
| `COGNITO_USER_POOL_ID` | local pool ID from the seed script | `us-east-1_2g8EYCEa1` |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | `local` / `local` | *(unset; ECS task role)* |
| `DB_HOST` / `DB_PORT` | `mysql` / `3306` (set by Compose) | RDS endpoint / `3306` |
| `DB_NAME` | `db_user` | `db_user` |
| `DB_USER` / `DB_PASSWORD` | from `.env` | from Secrets Manager |
| `DB_SSL_CA` | *(unset)* | `/etc/ssl/rds/global-bundle.pem` (bundled in the image) |
| `LOG_LEVEL` | `INFO` | `INFO` |
| `RABBITMQ_URL` (worker only) | `amqp://user-service:ticketto-local@rabbitmq:5672/%2F` (set by Compose) <!-- local-only password; gitleaks:allow --> | Secrets Manager `rabibi/service/user-service`, key `url` |

## Lint

```bash
ruff check .            # lint (add --fix to auto-fix)
ruff format .           # format (CI runs `ruff format --check .`)
python -m pylint app tests alembic
```

Both are configured in `pyproject.toml`, and CI fails on any message from either. The few deliberate exceptions are marked inline with a comment explaining why, e.g. Alembic's runtime-generated `op` module.

## CI/CD

The pipeline is in [`.gitlab-ci.yml`](.gitlab-ci.yml). It runs for merge requests and for `main`:

| Stage | Jobs | Notes |
|---|---|---|
| lint | `ruff`, `pylint` | ruff findings also appear in the MR's Code Quality widget |
| test | `unit-tests`, `integration-tests`, `sast`, `secret_detection` | Unit test results and coverage show in the MR. Integration tests run against MySQL and cognito-local as CI services ([`scripts/ci/run-integration-tests.sh`](scripts/ci/run-integration-tests.sh)) |
| build | `build-image` (MRs), `build-and-push` (`main`) | MRs only check the image builds; `main` pushes it to ECR |
| deploy | `migrate`, `deploy` (`main` only) | Runs `alembic upgrade head` as a one-off ECS task, then rolls the ECS service onto the new image |

A failure in an earlier stage stops the later ones, so nothing is deployed unless lint and all tests pass.

## Deploying to AWS

Every merge to `main` deploys automatically. The one-time AWS and GitLab setup is in [deploy/README.md](deploy/README.md): OIDC role, ECR, IAM roles, RDS secret, CI/CD variables, and the first-deploy order.

- **Migrations** run once per deployment as a one-off ECS task. The service container doesn't migrate on start.
- **Task definition:** [`deploy/task-definition.json`](deploy/task-definition.json) is the template; the pipeline fills in the image and environment-specific values.
- **Network:** port 8000 must only be reachable through API Gateway (the shared ALB). Other services call `/internal/v1` on port 8001 (`INTERNAL_PORT`), which serves nothing else, and must never be let in on 8000. RDS must only accept port 3306 from this service's security group.
