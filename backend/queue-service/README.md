# Queue Service

Waiting-room service for the ticketing platform. Buyers join a per-event queue. A single admitter moves them, first come first served, into a capped active pool as slots free up, and issues each admitted buyer a signed (RS256) admission token that the Reservation service validates.

- Contracts (source of truth): [`docs/Queue Service – API & Message Contracts.md`](docs/Queue%20Service%20–%20API%20&%20Message%20Contracts.md) and [`docs/Endpoint reference.md`](docs/Endpoint%20reference.md)
- Build plan and status: [`docs/implementation-plan.md`](docs/implementation-plan.md)
- Implementation rules and design notes: [`CLAUDE.md`](CLAUDE.md)

**Stack:** Python 3.12, FastAPI, MySQL, Redis, RabbitMQ (pika), PyJWT.

The service runs as three processes from the same codebase:

| Process | Role | Scaling |
| --- | --- | --- |
| `api` | HTTP API for buyers and internal callers | Can run several copies |
| `admitter` | Moves buyers into the pool about once a second | **Exactly one instance** |
| `consumer` | Handles RabbitMQ messages from Event, Reservation and Checkout | One per queue |

> **Status:** Phases 0–8 are built: the full service (buyer and internal APIs, admitter, RabbitMQ consumer, sale end) plus end-to-end tests, a Docker image, JSON logging, worker health checks and ops tools (see Operations below). Still to come: optional audit messages (Phase 9) and the move to AWS-hosted dependencies (Phase 10). Progress is tracked in the plan.

---

## Prerequisites

- [uv](https://docs.astral.sh/uv/) 0.11+ (`brew install uv`). It installs Python 3.12 for you if needed.
- Docker Desktop (or another Docker daemon), running. You need it for local MySQL/Redis/RabbitMQ and for the integration tests.
- `make` (optional; every target below also shows the raw command).

## First-time setup

```bash
uv sync                    # create .venv and install all dependencies (incl. dev tools) from uv.lock
cp .env.example .env       # local settings; values match docker-compose.yml
make keys                  # generate a local JWT signing key pair into secrets/
                           #   = uv run python -m scripts.gen_jwt_key
```

`make keys` writes `secrets/jwt_private.pem` and `secrets/queue-local-1.public.pem`. It won't overwrite existing keys. To replace them, or to use a different key ID:

```bash
uv run python -m scripts.gen_jwt_key --force
uv run python -m scripts.gen_jwt_key --kid queue-local-2 --out-dir secrets
```

`secrets/` and `.env` are gitignored. Never commit keys.

## Local dependencies (MySQL, Redis, RabbitMQ)

```bash
make up                    # start all three and wait until healthy
                           #   = docker compose up -d --wait
make down                  # stop them, and the app containers if up-app is running (MySQL data is kept)
                           #   = docker compose down
docker compose down -v     # stop and delete the MySQL volume (fresh database)
docker compose ps          # status / health
docker compose logs -f rabbitmq   # follow one service's logs
```

| Service | Address | Credentials |
| --- | --- | --- |
| MySQL 8.4 (UTC) | `localhost:3306`, database `queue_service` | `queue` / `queue` (root: `root`) |
| Redis 7.4 | `localhost:6379` | none |
| RabbitMQ 3.13 | `localhost:5672` (AMQP) | `queue` / `queue` |
| RabbitMQ management UI | http://localhost:15672 | `queue` / `queue` |

Handy shells:

```bash
docker compose exec mysql mysql -uqueue -pqueue queue_service
docker compose exec redis redis-cli
```

## Running the service

Directly on your machine (needs `make up` for the dependencies):

```bash
make migrate               # apply database migrations
                           #   = uv run alembic upgrade head
make api                   # HTTP API with auto-reload on http://localhost:8000; docs at /docs
                           #   = uv run uvicorn app.main:create_app --factory --reload
make admitter              # admitter tick loop; run exactly one; Ctrl-C / SIGTERM stops it cleanly
                           #   = uv run python -m workers.admitter
make consumer              # RabbitMQ consumers: declares the topology, consumes 3 queues; Ctrl-C stops cleanly
                           #   = uv run python -m workers.consumer
```

Typical local session: `make up`, then `make migrate`, then run `make api`, `make admitter` and `make consumer`, each in its own terminal.

Or everything in containers, using the same image as production:

```bash
make up-app                # build the image; start deps + migrate, api, admitter, consumer
docker compose --profile app ps                                  # health of every container
docker compose --profile app logs -f api admitter consumer       # follow the app logs
make down                  # stop it all
```

The containers read the signing key from `./secrets` (mounted read-only). If the API logs a permission error on Linux, the local dev key's `0600` mode is the cause; `chmod 644 secrets/jwt_private.pem` is fine **for that throwaway local key only**.

## Tests

Redis, MySQL and RabbitMQ are **never mocked**. Integration tests connect to a separate throwaway stack defined in `docker-compose.test.yml`. Only the other microservices (Event, Reservation, Checkout) are faked, under `tests/fakes/`.

The test stack is separate from the dev stack (`make up`), with its own project name and ports, so both can run at once:

| Service | Test address | Dev address |
| --- | --- | --- |
| MySQL | `127.0.0.1:13306` | `localhost:3306` |
| Redis | `127.0.0.1:16379` | `localhost:6379` |
| RabbitMQ (AMQP) | `127.0.0.1:15673` | `localhost:5672` |

It has no volumes (MySQL data lives in tmpfs), so `make test-down` discards everything.

```bash
make test-unit             # pure logic only, no Docker needed
                           #   = uv run pytest -m unit
make test-up               # start the test stack and wait until healthy
                           #   = docker compose -f docker-compose.test.yml up -d --wait
make test-integration      # runs test-up first, then the integration tests
                           #   = REQUIRE_TEST_SERVICES=1 uv run pytest -m "integration or e2e"
make test-down             # stop the test stack and discard its data
                           #   = docker compose -f docker-compose.test.yml down -v
```

The test stack can stay up between runs. Each test empties the tables and flushes Redis before it starts, and migrations are re-applied (as a no-op) on every run.

More pytest invocations:

```bash
uv run pytest tests/unit/test_gen_jwt_key.py          # one file
uv run pytest -k rabbitmq                             # tests matching a name
uv run pytest -x -vv                                  # stop at first failure, verbose
REQUIRE_TEST_SERVICES=1 uv run pytest                 # fail (instead of skip) if the test stack is down
```

If a test service isn't reachable, its tests are **skipped** with a message telling you to run `make test-up`. CI sets `REQUIRE_TEST_SERVICES=1`, so there they fail instead.

To point the tests somewhere else, set `TEST_{MYSQL,REDIS,RABBITMQ}_{HOST,PORT}`. CI does this to use its own services.

Test markers: `unit` (no I/O), `integration` (real services), `e2e` (full flow across all three processes), `contract` (AMQP message contracts, below).

### Message contract tests

`tests/contract` checks Queue's side of the AMQP message contracts owned by rabbitmq-service:
that the consumer listens on exactly its catalogued queues, acks every body the publishers
always send (and bodies with fields it doesn't know), and dead-letters a body missing a required
field. The tests are generated by rabbitmq-service's `ticketto-contracts` package from its
contract catalogue; `tests/contract/conftest.py` connects them to Queue's real queue list and
its real accept/reject decision (`dispose`). No Docker needed.

The package comes from rabbitmq-service's private GitLab package registry, so it is in its own
dependency group, `contract`, and needs a GitLab personal access token with `read_api`:

```bash
export UV_INDEX_TICKETTO_USERNAME=__token__
export UV_INDEX_TICKETTO_PASSWORD=<your token>
make test-contract         # = uv run --group contract pytest tests/contract -m contract
```

Without the package the contract tests are skipped, so `make test-unit`, `make check` and the
integration image need no token. To move to a new contract version, change
`ticketto-contracts==X.Y.Z` in `pyproject.toml` and run `uv lock` (with the token set).
What the versions contain: rabbitmq-service's `contracts/CHANGELOG.md`.

## Lint, format, type-check

```bash
make check                 # lint, format check and strict mypy (what CI runs)
                           #   = uv run ruff check . && uv run ruff format --check . && uv run mypy
make fmt                   # auto-fix lint issues and reformat
                           #   = uv run ruff check --fix . && uv run ruff format .
```

`uv run pytest` runs everything at once; integration tests are skipped if the test stack is down.

## Dependencies

```bash
uv add <package>           # runtime dependency
uv add --dev <package>     # dev/test dependency
uv sync                    # sync .venv after pulling changes to uv.lock
uv lock --upgrade          # upgrade all locked versions
```

## CI

`.gitlab-ci.yml` runs on every push:

1. **static_analysis**: ruff, pylint, mypy, SAST, secret detection, dependency audit.
2. **test**: `integration-tests`, the full `pytest` suite against MySQL, Redis and RabbitMQ in
   docker compose (`ci/compose.test.yaml`); and `contract-tests`, the message contract tests,
   which installs `ticketto-contracts` with the job's own token.
3. **release** and **deploy** (manual).

rabbitmq-service also triggers this pipeline for every contract merge request that affects
Queue, with `CONTRACTS_VERSION` set to that merge request's pre-release. Those pipelines run
only `contract-tests`, against that pre-release, and a failure blocks the contract change.

## Configuration

Settings come from environment variables (or `.env`):

| Variable | Example | Purpose |
| --- | --- | --- |
| `MYSQL_URL` | `mysql+asyncmy://queue:queue@localhost:3306/queue_service` | Queue configuration store |
| `REDIS_URL` | `redis://localhost:6379/0` | Queues, active pools, admissions |
| `RABBITMQ_URL` | `amqp://queue:queue@localhost:5672/%2F` | Message broker |
| `JWT_PRIVATE_KEY_PATH` | `secrets/jwt_private.pem` | Admission-token signing key |
| `JWT_KID` | `queue-local-1` | Key ID in token headers and JWKS |
| `JWT_PUBLIC_KEYS_DIR` | *(empty)* | Extra public keys kept in JWKS during key rotation |
| `ADMITTER_TICK_SECONDS` | `1` | Admitter loop interval |
| `LOG_LEVEL` | `INFO` | Log verbosity |
| `LOG_FORMAT` | `json` | `json` (one object per line, production) or `text` (local terminals) |

Local development and tests use the Docker containers above. Moving Redis, MySQL and RabbitMQ to AWS is a later phase (Phase 10 in the plan).

## Operations (runbook)

### Processes and deployment order

All three processes come from one image (`Dockerfile`); the command chooses which.

| Process | Command | Replicas | Health |
| --- | --- | --- | --- |
| migrate | `alembic upgrade head` | one-off, **before** each deploy | exit code |
| api | *(image default)* uvicorn on :8000 | as many as needed (stateless) | `GET /health/live`, `GET /health/ready` (MySQL + Redis) |
| admitter | `python -m workers.admitter` | **exactly 1**, never more | `python -m workers.healthcheck admitter` |
| consumer | `python -m workers.consumer` | 1 or more (handlers are idempotent; quorum queues share the work) | `python -m workers.healthcheck consumer` |

Deploy order: run migrate, then roll the api, admitter and consumer. All three stop cleanly on SIGTERM: the admitter finishes its tick, and consumers finish their current message, so rolling restarts lose nothing. A consumer exits with code 1 if one of its queue threads crashes, so the orchestrator restarts it.

**Never run two admitters.** The Lua scripts keep the pool safe even so, but the shuffle timing and logs assume a single instance.

### Health

- **API:** `/health/ready` returns 503 naming the failed dependency, e.g. `{"status": "unavailable", "failed": ["redis"]}`.
- **Workers:** they write heartbeats to Redis. The admitter writes `queue:admitter:heartbeat` every tick; each consumer queue writes `queue:consumer:heartbeat:<queue>` every 5 s. The healthcheck exits 1 if a heartbeat is missing or stale:

  ```bash
  python -m workers.healthcheck admitter     # or: consumer
  ```

### Logs

`LOG_FORMAT=json` gives one JSON object per line with context fields such as `event_id`, `admission_id`, `message_id` and `outcome`. Useful messages:

| Message | Meaning |
| --- | --- |
| `queue opened`, `pre-queue shuffled`, `admit`, `sale ended; queue closed`, `drained` | Admitter lifecycle |
| `slow admitter tick` | A tick took more than 80% of its interval. Look at event count and Redis latency. |
| `message handled` (`outcome=...`) | One consumed message and what happened to it |
| `event_update_rejected` | Event changed config after the lobby opened. Ignored by design; tell the Event team. |
| `admission_integrity_mismatch` | OrderConfirmed's user/event didn't match the admission. Investigate with Checkout. |
| `poison message dead-lettered` / `message handling failed; will be retried` / `message handling failed; dead-lettered after max attempts` | See *Dead letters* below |

### Dead letters

Malformed messages are dead-lettered at once. Messages whose handler keeps failing are retried after 1, 2, 4 and 8 seconds, then dead-lettered on the 5th attempt. Each consumer queue has its own `.dlq`.

```bash
uv run python -m workers.dlq list                          # depth of every DLQ
uv run python -m workers.dlq peek EventPublished -n 5      # read without removing (may reorder the DLQ)
uv run python -m workers.dlq replay EventPublished         # after fixing the cause
uv run python -m workers.dlq replay EventSoldOut --limit 10
```

Replay sends messages straight back to Queue's own consumer queue (other services don't see them again). The attempt counter starts fresh. Handlers are idempotent, so replaying something that was already applied is harmless.

### Signing-key rotation

Tokens are signed on every poll, so a rotation takes effect on the next poll. Reservation refetches the JWKS when it sees an unknown `kid`.

1. Generate the new pair: `python -m scripts.gen_jwt_key --kid queue-2026-11 --out-dir <new-dir>`, or use your secret store.
2. Put the **old** public key in `JWT_PUBLIC_KEYS_DIR` as `<old-kid>.pem`, so it stays in the JWKS.
3. Point `JWT_PRIVATE_KEY_PATH` and `JWT_KID` at the new key and roll the API. New tokens now carry the new `kid`.
4. **After at least 11 minutes** (the longest admission), remove the old public key file and roll the API again.

### Load smoke

Against a running stack (`make up-app`, or the three `make` targets):

```bash
uv run python -m scripts.load_smoke --buyers 2000 --release-in 30 --duration 120
uv run python -m scripts.load_smoke --buyers 20000 --concurrency 500 --duration 300   # plan target
```

It publishes an event, has buyers join and poll at the server-directed intervals, confirms orders for admitted buyers, and prints p50/p95/p99 latency per endpoint plus how many buyers got in.

## Project layout

```
app/                  # FastAPI app, core, queue logic, messaging
workers/              # process entrypoints: admitter, consumer; ops: healthcheck, dlq
scripts/              # dev utilities: gen_jwt_key.py, wait_for_ports.py, load_smoke.py
tests/
  conftest.py         # fixtures connecting to the test stack (Redis, MySQL, RabbitMQ)
  fixtures/           # test keys, data factories
  fakes/              # fake Event / Reservation / Checkout services
  unit/  integration/  e2e/
docs/                 # contracts, endpoint reference, implementation plan
Dockerfile            # one image for api / admitter / consumer / migrate
docker-compose.yml    # local dev MySQL, Redis, RabbitMQ (+ the app, with --profile app)
docker-compose.test.yml  # throwaway test MySQL, Redis, RabbitMQ (other ports)
Makefile              # command shortcuts
```
