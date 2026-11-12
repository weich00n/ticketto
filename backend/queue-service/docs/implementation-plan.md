# Queue Service – Implementation Plan

Step-by-step build plan for the Queue Service. Contracts come from "Queue Service – API & Message Contracts" and its "Endpoint reference" tab; implementation decisions come from `CLAUDE.md`. Where this plan adds a decision those documents don't make, it says so in **Decisions made in this plan** below.

The repo is greenfield, so the phases build bottom-up: shared core, then the Redis layer (where correctness lives), then the three processes that use it. Each phase ends in a working, tested state and can be merged on its own.

---

## Status (updated 2026-10-17)

| Phase | Status | Verified |
| --- | --- | --- |
| 0 – Scaffolding and local infrastructure | **Done** | Lint, mypy and unit tests pass. Dev compose stack came up healthy. |
| 1 – Core: settings, errors, persistence, time, status | **Done** | Unit tests pass. Integration tests run by the developer on the compose test stack (2026-10-17). Migration applied to the dev compose MySQL. |
| 2 – Redis layer and Lua scripts | **Done** | Integration tests (93 total) run by the developer on the compose test stack (2026-10-17). |
| 3 – Admission tokens | **Done** | 25 new unit tests (98 unit tests total), lint and mypy pass. No Docker needed. |
| 4 – Buyer API | **Done** | Integration tests (116 total) run by the developer on 2026-10-17. |
| 5 – Internal API, JWKS, health | **Done** | 143 unit tests. Integration tests (133 total) run by the developer on 2026-10-17. |
| 6 – Admitter worker | **Done** | 148 unit tests. Integration tests (144 total) run by the developer on 2026-10-17. |
| 6b – Sale end (`sale_ends_at`) | **Done** | Integration tests (151 total) run by the developer on 2026-10-17. |
| 7 – RabbitMQ consumer | **Done** | 186 unit tests. Integration tests (187 total) run by the developer on 2026-10-17. |
| 8 – End-to-end, load and operations | **Code complete, awaiting your test run** | 201 unit tests (15 new), lint and mypy pass. 9 new integration/e2e tests (2 fixed after the first run), a Docker build and a load smoke run still to do. |
| 9 – Publish admission messages (optional) | Not started | — |
| 10 – Deploy to AWS (ECS, shared RDS, central RabbitMQ, Redis on EC2) | Not started; step-by-step plan written 2026-10-20 | — |

**Pending checks**
- [x] **Run the integration tests on the compose test stack** (Phases 1–2). Run by the developer on 2026-10-17.
- [ ] **Concurrency test ×20:** `CONCURRENCY_REPEATS=20 uv run pytest tests/integration/redis/test_admit_concurrency.py`. Not confirmed yet.
- [x] **Phase 4 API integration tests:** 116 passed (developer, 2026-10-17).
- [x] **Phase 5 integration tests:** 133 passed (developer, 2026-10-17).
- [x] **Phase 6 integration tests:** 144 passed (developer, 2026-10-17).
- [x] **Phase 6b integration tests:** 151 passed (developer, 2026-10-17).
- [ ] **Phase 8 checks** (yours):
  1. `make test-integration`: expect 196 passed (187 + 9 new, including the 2 end-to-end scenarios, which take ~15 s). First run (2026-10-17): 2 failures, both test assumptions, fixed. See the notes below.
  2. `make up-app`, then `docker compose --profile app ps`: migrate exited 0; api, admitter and consumer `healthy`. Then `make down`.
  3. Optional: `uv run python -m scripts.load_smoke --buyers 2000` against that stack.
- [x] **Phase 7 integration tests:** 187 passed (developer, 2026-10-17), including the real-broker confirmation that `x-delivery-limit: 4` gives exactly 5 attempts.
- [ ] **First CI run.** Nothing has been committed or pushed yet. `.gitignore` now also ignores `.gitlab-ci.yml` and `docs/*`, so as things stand the pipeline won't run on GitLab and this plan stays local.

**Working agreement:** Claude runs only unit tests, lint and mypy. Anything that starts containers (`make up`, `make test-up`, `make test-integration`) is run by the developer, and Claude lists the exact commands at the end of each phase.

---

## Decisions confirmed with the product owner (2026-10-09)

| Topic | Decision |
| --- | --- |
| Tooling | Python 3.12, **uv** (pyproject + `uv.lock`), **SQLAlchemy 2.0 async** with the **asyncmy** driver, **Alembic** migrations, **redis.asyncio**, pika, PyJWT + cryptography, pytest + pytest-asyncio, docker-compose for local MySQL/Redis/RabbitMQ (`docker-compose.yml` for dev, `docker-compose.test.yml` for tests). |
| `/health/ready` | The API checks **MySQL + Redis only**. The admitter and consumer report health through a heartbeat (see Phase 8), not HTTP. The spec's line "MySQL, Redis and the RabbitMQ connection" is to be updated. |
| Sold out (CLOSED) | Docs unchanged. On EventSoldOut **no new admissions** happen; existing admissions stay until order or 11-minute deadline. A still-admitted buyer who calls `POST /queue/entries` during CLOSED gets **200 with the existing admission**. Anyone without an admission gets 410 EVENT_SOLD_OUT. A fresh `admission_id` is only issued when a buyer rejoins after their admission ended while the queue is still ADMITTING. |
| Plan location | This file. |
| Sale end (2026-10-17) | Event sends an **optional** `sale_ends_at` in EventPublished/EventUpdated (null = no end; additive, schema_version stays 1). Once it passes, the queue becomes **CLOSED** (no new status). Joining then gets **410 `SALE_ENDED`**, distinct from `EVENT_SOLD_OUT`. Admitted buyers **keep their admission until order or deadline**; waiting buyers are cleared, as for a sell-out. Specs and CLAUDE.md updated. |

## Decisions made in this plan (review these)

These fill gaps in the spec. None changes a domain rule, but flag any you disagree with.

1. **Terminal status is enforced inside Lua, not only in Python.** The EventSoldOut/EventCancelled handlers write `status` into `queue:{eid}:config` *before* deleting keys, and the join and admit scripts read that field atomically. This closes the race where a join or admit slips in between the Python status check and the cleanup. The admit script also refuses to admit until the `shuffled` flag is set.
2. **Draining CLOSED events.** CLAUDE.md removes a CLOSED event from `queue:live_events`, but its remaining admissions still need the 11-minute sweep (so `user_adm` is cleaned up, the admission index is cleared and `AdmissionReleased` can fire later). The event moves to a new set, **`queue:draining_events`**. The admitter runs sweep-only on these and removes the event once `active` is empty. As a second line of defence, every read treats an admission whose `admission_expires_at <= now` as ended.
3. **Promotion to OPEN.** On each tick the admitter runs one indexed query: `status = 'SCHEDULED' AND release_at <= now + 15 min`. For each row it persists OPEN, refreshes the config cache and `SADD queue:live_events`. The existing `idx_status_release` index covers this, so no extra index is needed. The API doesn't depend on this step: it derives status with `effective_status()`, so a join at 11:45:00.2 works even if the tick hasn't run yet.
4. **Server time.** `server_now()` reads Redis `TIME`. Lua scripts call `redis.call('TIME')` themselves. Python code never uses `datetime.now()` for domain decisions.
5. **Config cache is read-through.** On a miss for `queue:{eid}:config`, load from MySQL and repopulate. A MySQL miss means 404 EVENT_NOT_FOUND. Consumers write the cache after their MySQL commit.
6. **Message idempotency.** Handlers are *naturally* idempotent: version checks for MySQL, and the Redis side effects (deletes, release) are safe to repeat. Order of operations: MySQL transaction (state change) → Redis side effects → ack. (`processed_messages` used to record each `message_id` in that transaction; it was dropped on 2026-10-21, migration `0004`, since it was only an audit record.) If Redis fails after the MySQL commit, the redelivered message finds the row already updated and **re-applies the Redis side effects** instead of skipping them. For example, a duplicate EventCancelled still re-runs key deletion.
7. **Retries and DLQ.** Quorum queues are declared with `x-delivery-limit: 5` and `x-dead-letter-exchange: queue-service.dlx`, so RabbitMQ counts the attempts. Transient failures → `basic_nack(requeue=True)`. Malformed envelope, unknown `message_type` or `schema_version > 1` → `basic_nack(requeue=False)`, which dead-letters immediately.
8. **Out-of-order or unknown events.** EventUpdated for an unknown `event_id` is handled as an upsert, since it carries full current values. EventCancelled, EventSoldOut and OrderConfirmed for an unknown event are logged and acked.
9. **Pagination cursors** are opaque base64url JSON. `/internal/v1/queues` uses a keyset on `(release_at, event_id)` with an optional status filter. Admissions use a keyset on `(admission_expires_at score, admission_id)` from the `active` ZSET. Because every admission lasts exactly 11 minutes, that order is also oldest-admitted first. The status filter uses the *persisted* status, which may lag the derived status by up to one tick. The returned `queue_status` is always `effective_status()`.
10. **Key rotation config.** CLAUDE.md lists only `JWT_PRIVATE_KEY_PATH` and `JWT_KID`. To serve an old key during the 11-minute overlap, add **`JWT_PUBLIC_KEYS_DIR`** (optional): every `<kid>.pem` in it is published in JWKS alongside the active key. Tokens are re-signed on each poll, so after a rotation a buyer's next poll returns a token under the new kid. Both tokens remain valid.
11. **Safety TTL.** Every script that creates per-event keys sets `PEXPIREAT` to `release_at + 3 days` (passed as ARGV). On EventSoldOut, the remaining active-pool keys get `PEXPIREAT` = max `admission_expires_at` + 1 hour.
12. **`GET /queue/me` while SCHEDULED** returns `entry_status: NONE`, `poll_after_seconds: null` (the client uses the lobby endpoint's countdown).

Added during Phase 1:

13. **The config cache never goes backwards.** Every cache write goes through `queue/lua/cache_config.lua`. It refuses to replace a terminal status with a live one, to replace CANCELLED with anything else, or to overwrite a newer `event_version`. CLOSED → CANCELLED is still allowed. Without this, a read-through that loaded OPEN from MySQL just before a sold-out commit could put OPEN back into the cache, and decision 1 relies on the cached status.
14. **Status transitions only move forward in MySQL.** `set_status` only updates when the current status is an allowed predecessor: OPEN ← SCHEDULED; ADMITTING ← SCHEDULED/OPEN; CLOSED ← SCHEDULED/OPEN/ADMITTING; CANCELLED ← anything except CANCELLED. A disallowed transition returns `False` and changes nothing, so the admitter persisting ADMITTING can never overwrite a CLOSED written by the consumer.
15. **Error codes the spec doesn't list:** 400 `INVALID_REQUEST` for malformed query/body parameters (with `details.errors`), 404 `NOT_FOUND` and 405 `METHOD_NOT_ALLOWED` for unknown routes and methods, and 500 `INTERNAL_ERROR` for unhandled exceptions (no internals leaked). A malformed `event_id`/`admission_id` path parameter still maps to the spec's 404 codes.
16. **Malformed pagination cursor:** the repository raises `InvalidCursor`, which the API maps to 400 `INVALID_REQUEST` (done in Phase 5).
17. **Test infrastructure via compose, not testcontainers.** At the developer's request, integration tests connect to `docker-compose.test.yml`: MySQL 13306, Redis 16379, RabbitMQ 15673, separate from the dev stack's ports, with MySQL data in tmpfs. The developer starts the stack. Unreachable services are skipped locally and fail under `REQUIRE_TEST_SERVICES=1`. CI runs the same images as GitLab job services, so it needs no docker-in-docker.

---

## Environments: containers first, AWS later

Phases 0–9 are built and tested entirely against **temporary Docker containers**: Redis, MySQL and RabbitMQ via `docker-compose.yml` for local runs and `docker-compose.test.yml` for pytest. No AWS resources are needed until Phase 10, which deploys the service onto AWS once everything works locally: the processes on ECS, our own database in a shared RDS instance, the centralised RabbitMQ broker, and Redis on our own EC2 instance.

To make Phase 10 a configuration change rather than a rewrite, the earlier phases keep just this groundwork. Each item costs almost nothing up front:

- All connection details come from env URLs (`MYSQL_URL`, `REDIS_URL`, `RABBITMQ_URL`), so pointing at AWS later means changing env vars, not code.
- Secrets (DB passwords, the JWT private key) are read from env or mounted files, never hard-coded or baked into the image.
- Per-event Redis keys stay hash-tagged and each Lua script only touches one event's keys (already required by CLAUDE.md).
- Migrations run as an explicit command (`uv run alembic upgrade head`), not on app start.

---

## Target layout

```
pyproject.toml  uv.lock  alembic.ini  docker-compose.yml  docker-compose.test.yml  Dockerfile  .env.example
app/
  main.py                    # create_app(): routers, exception handlers, lifespan (pools)
  api/buyer.py  api/internal.py  api/deps.py  api/errors.py  api/schemas.py
  core/config.py             # pydantic-settings
  core/db.py                 # async engine/session, models
  core/redis.py              # client, server_now() via TIME
  core/timefmt.py            # ISO 8601 / epoch-ms helpers (Phase 1)
  core/cursor.py             # opaque pagination cursors (Phase 1)
  core/tokens.py             # RS256 sign, JWKS, key loading (Phase 3)
  core/status.py             # effective_status(), poll_after_seconds(), pool capacity
  core/logging.py
  queue/keys.py              # all key names, hash tag {eid}
  queue/repository.py        # Lua wrappers + Redis reads + event cleanup (Phase 2)
  queue/models.py            # Admission, Entry and script result types (Phase 2)
  queue/config_repo.py       # MySQL queue_configs access + cache
  queue/service.py           # join/poll/leave orchestration used by API
  queue/admin.py             # read-only views for /internal/v1 (Phase 5)
  queue/lua/cache_config.lua (Phase 1)  join.lua shuffle.lua admit.lua release.lua leave.lua
  messaging/topology.py  messaging/schemas.py  messaging/handlers.py  messaging/consumers.py  (Phase 7)
  messaging/publisher.py     # (Phase 9, optional)
workers/admitter.py  workers/consumer.py  workers/healthcheck.py  workers/dlq.py  (Phase 8: ops tools)
migrations/                  # alembic
scripts/gen_jwt_key.py       # local dev key pair
scripts/wait_for_ports.py    # CI: wait for job services
tests/                       # see "Test layout" below
```

### Test layout (pytest)

**Rule:** the **other microservices** (Event, Reservation, Checkout) are always mocked. **Infrastructure** (Redis, MySQL, RabbitMQ) is always real: the throwaway stack in `docker-compose.test.yml`, which the developer starts with `make test-up` (CI uses equivalent job services). Never mocks, because the Lua scripts' atomicity and the MySQL locking can only be proven against the real thing. Tests never touch the AWS-hosted dependencies.

Queue makes no outbound calls to other services. They only reach it through RabbitMQ messages, and Reservation only reads the JWKS endpoint. So "mocking a service" means a test double that produces that service's messages or plays its validation role.

```
tests/
  conftest.py                  # connects to the test stack (Redis, MySQL, RabbitMQ), alembic upgrade once,
                               # Settings override (container URLs, test RSA key, short deadlines),
                               # per-test isolation: FLUSHDB, TRUNCATE tables, fresh vhost or queue purge
  fixtures/
    keys.py                    # test RSA key pair(s) generated at session start, incl. a second kid for rotation tests
    factories.py               # builders: queue_config(...), admission(...), user_id(), event_id()
  fakes/                       # mocks of the OTHER microservices
    messages.py                # envelope + payload builders for every consumed message (valid, stale-version,
                               # duplicate message_id, malformed, unknown type, schema_version 2)
    event_service.py           # FakeEventService: publish(EventPublished|EventUpdated|EventCancelled) to event.events
    reservation_service.py     # FakeReservationService: publish EventSoldOut; validate_token(token, user, event)
                               # using the same rules as Reservation (JWKS by kid, RS256 only, claims, selection deadline)
    checkout_service.py        # FakeCheckoutService: publish OrderConfirmed (matching / mismatched / unknown admission)
    queue_events_sink.py       # (Phase 9) test queue bound to queue.events to capture AdmissionGranted/Released
  unit/                        # no I/O, no containers
    test_status.py             # capacity, effective_status, poll_after_seconds
    test_tokens.py             # deterministic signing, claims, JWKS shape, rotation
    test_schemas.py            # envelope/payload validation, poison detection
    test_cursor.py  test_timefmt.py  test_errors.py  test_config.py  test_gen_jwt_key.py
  integration/                 # real Redis/MySQL/RabbitMQ from the test stack
    test_services.py           # smoke: each test service reachable (MySQL in UTC, quorum queues)
    redis/  conftest.py (repo, short_repo, open_event, admitting_event)
            test_server_time.py  test_join.py  test_shuffle.py  test_admit.py  test_admit_concurrency.py
            test_release.py  test_leave.py  test_cleanup.py  test_reads.py  test_multi_event.py
    db/     test_migrations.py  test_config_repo.py
    api/    test_buyer.py  test_internal.py  test_health.py  test_errors_503.py
    messaging/  test_handlers.py   # handlers called directly with fakes/messages.py envelopes
                test_topology.py   # declare twice, DLX/DLQ wiring, delivery limit
                test_consumer.py   # fakes publish via RabbitMQ → consumer threads → effects / DLQ
    workers/    test_admitter.py
  e2e/
    test_full_flow.py          # api + admitter + consumer in-process, driven only by the fakes
```

**Conventions**
- Markers: `unit`, `integration`, `e2e`, registered in `pyproject.toml` (`[tool.pytest.ini_options]`, `asyncio_mode = "auto"`). `uv run pytest -m unit` runs without Docker. CI runs everything.
- API tests use `httpx.AsyncClient(transport=ASGITransport(app))` against `create_app()` with test settings. No uvicorn.
- **Time:** domain time comes from Redis `TIME`, so tests don't fake the clock. Instead they create configs with `release_at` relative to `server_now()` (e.g. now − 1 s to be ADMITTING immediately). Admission deadlines (5/11 min) are settings constants passed to the scripts via ARGV. Tests override them to seconds. Production always uses the fixed values, and a unit test asserts the defaults.
- **Randomness:** `secrets.SystemRandom` and the poll jitter are injected (`rng` parameter), so tests can pass a seeded `random.Random`.
- **503 tests** stop or pause the container (or point the settings at a closed port) rather than mocking the client.
- Each fake service records what it sent, so tests can assert on it, and uses the real envelope format and exchange/routing key from the contracts doc. A contract change then breaks the tests, as it should.

---

## Phase 0 – Project scaffolding and local infrastructure

**Steps**
1. `uv init`, then add dependencies: fastapi, uvicorn[standard], pydantic, pydantic-settings, sqlalchemy[asyncio], asyncmy, alembic, redis, pika, pyjwt[crypto], cryptography. Dev dependencies: pytest, pytest-asyncio, httpx, ruff, mypy. Create the `tests/` skeleton (conftest connecting to the test stack, `fixtures/`, `fakes/`) and register the pytest markers.
2. `docker-compose.yml` with MySQL 8, Redis 7 and RabbitMQ 3.13 (management plugin, quorum queues supported), with healthchecks.
3. `.env.example` with `MYSQL_URL`, `REDIS_URL`, `RABBITMQ_URL`, `JWT_PRIVATE_KEY_PATH`, `JWT_KID`, `JWT_PUBLIC_KEYS_DIR`, `ADMITTER_TICK_SECONDS=1`, `LOG_LEVEL`. AWS-specific settings are added in Phase 10.
4. `scripts/gen_jwt_key.py` to generate an RSA-2048 PEM pair into `./secrets/` (gitignored).
5. Ruff + mypy config. Add a `Makefile` or `uv run` task aliases: `api`, `admitter`, `consumer`, `test`, `migrate`.
6. CI workflow: lint, type-check, then tests against MySQL/Redis/RabbitMQ job services.

**Done when** `docker compose up` brings up healthy dependencies and `uv run pytest` runs an empty suite in CI.

**Status: done (2026-10-14).**
- Built: `pyproject.toml` + `uv.lock`, `.python-version`, `docker-compose.yml` (MySQL 8.4 in UTC, Redis 7.4, RabbitMQ 3.13 with management UI), `.env.example`, `scripts/gen_jwt_key.py` (`make keys`), `Makefile`, `.gitlab-ci.yml`, `README.md`, and the `tests/` skeleton (`conftest.py`, `fixtures/keys.py`, `fakes/` placeholder).
- Changed from the plan: CI is GitLab (`.gitlab-ci.yml`), because the remote is GitLab. testcontainers was replaced by `docker-compose.test.yml` (decision 17). Ruff is configured to skip `*.md`, because it otherwise reformats Python code blocks inside Markdown.
- Not yet verified: the first CI run (nothing pushed yet).

---

## Phase 1 – Core: settings, errors, persistence, time, status

**Steps**
1. `core/config.py`: `Settings` (pydantic-settings) with the env vars above. Cached `get_settings()`.
2. `api/errors.py`: an `AppError(status, code, message, details)` hierarchy (`EventNotFound`, `QueueNotOpen(queue_opens_at)`, `EventSoldOut`, `EventCancelled`, `NotInQueue`, `AlreadyAdmitted`, `AdmissionNotFound`, `Unauthenticated`, `ServiceUnavailable`). Exception handlers render `{"error": {...}}`. Map `redis.ConnectionError`/`TimeoutError` and SQLAlchemy `OperationalError`/`InterfaceError` to 503 SERVICE_UNAVAILABLE. Map FastAPI validation errors on path UUIDs to the same body shape: 404 EVENT_NOT_FOUND for a bad `event_id`, 404 ADMISSION_NOT_FOUND for a bad `admission_id`.
3. `core/db.py`: async engine and session factory. `QueueConfig` and `ProcessedMessage` models matching the CLAUDE.md schema. UTC handling: store naive UTC `DATETIME(3)` and convert to aware UTC at the boundary.
4. Alembic: initial migration creating `queue_configs` (with `idx_status_release`) and `processed_messages`.
5. `core/redis.py`: client factory (`decode_responses=True`) and `server_now()` built on `TIME`, returning an aware UTC datetime with ms precision.
6. `core/status.py` (pure functions, no I/O):
   - `active_pool_capacity(total) = max(1, total // 5)`
   - `queue_opens_at(release_at) = release_at - 15 min`
   - `effective_status(cfg, now)`: terminal statuses pass through. Otherwise `now < queue_opens_at` → SCHEDULED, `now < release_at` → OPEN, else ADMITTING.
   - `poll_after_seconds(queue_status, entry_status, position, capacity, now, release_at, rng)` per the CLAUDE.md table. OPEN = `min(30, ceil(seconds to release))` + `randint(0, 5)`.
   - Shared ISO 8601 formatter producing `YYYY-MM-DDTHH:MM:SSZ`, matching the examples.
7. `queue/config_repo.py`: `get(eid)` (cache, falling back to MySQL), `upsert_cache`, `set_status`, `list(status, limit, cursor)`, `due_for_open(now)`.

**Tests (unit):** capacity examples (304→60, 3→1, 0→1, 4→1, 5→1), `effective_status` boundaries (exactly at opens_at, at release_at, terminal overrides), every `poll_after_seconds` row including the cap near release and the jitter range, timestamp formatting.

**Done when** migrations apply to compose MySQL and unit tests pass.

**Status: done (2026-10-14).**
- Built: `app/core/{config,status,timefmt,cursor,db,redis}.py`, `app/api/errors.py`, `app/queue/{keys,config_repo}.py`, `app/queue/lua/cache_config.lua`, `alembic.ini`, `migrations/` (revision `0001`, matching the CLAUDE.md schema).
- Tests: 73 unit tests (status rules, time formatting, cursors, settings, error bodies) and 38 integration tests (schema and index, `alembic check` model parity, upgrade/downgrade round trip, cache read-through and staleness rules, status transitions, paging, `due_for_open`, Redis `TIME`).
- Changed from the plan:
  - `list(...)` is named `list_configs(...)`, so it doesn't shadow the builtin.
  - The repository returns a frozen `QueueConfigData` dataclass, not ORM rows.
  - The settings also carry `selection_window_seconds` and `admission_window_seconds` (defaults 300/660; overridable only so tests can shrink them).
  - Decisions 13–16 were added.
- Verified: `alembic upgrade head` applied to the dev compose MySQL (tables present, time zone `+00:00`). The integration tests still need a re-run on the compose test stack (see **Pending checks**).

---

## Phase 2 – Redis layer and Lua scripts (the critical phase)

All per-event keys go through `queue/keys.py` (`queue:{eid}:…`). Every multi-key script receives only keys from one event, so it stays Redis Cluster-safe. Scripts are loaded with `register_script` at startup.

**Steps**
1. **join.lua.** KEYS: config, user_adm, prequeue, waiting, seq, shuffled. ARGV: user_id, ttl_at.
   - config `status` ∈ {CLOSED, CANCELLED} → `{"closed", status}` (closes the race described in decision 1).
   - user in `user_adm` → `{"admitted", admission_id}`.
   - in `waiting` → `{"waiting", rank}`. In `prequeue` → `{"prequeue"}`.
   - otherwise, if `shuffled` exists: `INCR seq`, `ZADD waiting`. Else `SADD prequeue`. Set TTLs and return `{"created", …}`.
2. **shuffle.lua.** KEYS: shuffled, prequeue, waiting, seq. ARGV: ttl_at, then the ordered user_ids. `SET shuffled 1 NX`, aborting if it was already set. For each ARGV user still in `prequeue`: `INCR seq`, `ZADD waiting`. Then append any `SMEMBERS prequeue` leftovers not yet added, then `DEL prequeue`. Python does `SMEMBERS` and `secrets.SystemRandom().shuffle()` before calling.
   - Leftovers are appended in `SMEMBERS` order, which is arbitrary but acceptable: they joined during the same pre-release window. Document this.
3. **admit.lua.** KEYS: config, active, user_adm, waiting, shuffled, plus a prefix for admission hashes. Admission hash keys are built inside the script from the hash-tagged prefix, so they stay in the same slot. ARGV: capacity, ttl_at, N pre-generated UUIDs.
   1. `now` from `TIME`. **Sweep:** `ZRANGEBYSCORE active -inf now` → for each id, read `user_id` from the hash, `HDEL user_adm`, `DEL` the hash, `ZREM active`. Collect the released entries `(id, user_id)`.
   2. If config status is terminal or `shuffled` is missing → return the sweep result only.
   3. `free = capacity - ZCARD active`. `ZPOPMIN waiting min(free, #uuids)`.
   4. For each popped user: write the admission hash (ISO strings plus `admitted_at_ms`), `HSET user_adm`, `ZADD active <admission_expires_at_ms> id`, and set TTLs.
   5. Return `{released, admitted}`. Python then writes or deletes `queue:admission_index` entries for both lists.
   - Python passes `capacity` UUIDs. That is enough, because `free <= capacity`.
4. **release.lua** (OrderConfirmed). KEYS: active, user_adm, admission hash. ARGV: admission_id, user_id, event_id. Not in `active` → `not_found`. User or event mismatch → `mismatch`. Else delete everything → `released`.
5. **leave.lua.** Admitted → `already_admitted`. `ZREM waiting` or `SREM prequeue` → `left`. Else `not_in_queue`.
6. **Event cleanup** (plain Python pipelines, not scripts):
   - `cancel_event(eid)`: `HSET config status CANCELLED`, collect admission ids from `active`, `DEL` every per-event key (prequeue, waiting, seq, shuffled, active, user_adm, adm hashes), `HDEL queue:admission_index`, `SREM live_events` / `draining_events`.
   - `close_event(eid)`: `HSET config status CLOSED`, `DEL prequeue waiting`, set PEXPIREAT on the active-pool keys, `SREM live_events`. If `active` is non-empty, `SADD draining_events`.
7. **Reads in `repository.py`:** `get_entry(eid, uid)` (pipeline: `HGET user_adm`, `ZRANK waiting`, `SISMEMBER prequeue`, then `HGETALL` of the admission), `counts(eid)` (ZCARD waiting + SCARD prequeue, ZCARD active), `list_admissions(eid, cursor, limit)`, `get_admission(aid)` (via index, lazily cleaning stale entries).

**Tests (integration, real Redis from the test stack). These are the CLAUDE.md priorities:**
- join idempotency: repeated joins return the same state. A join after release appends FIFO.
- shuffle runs exactly once (second call aborts). Joins that land mid-shuffle are not lost. Every prequeue member gets exactly one seq.
- Admit fills up to capacity at release, then exactly one buyer per freed slot.
- **Concurrency:** 50 parallel admit calls plus parallel releases against capacity 5 with 1,000 waiting. Assert `ZCARD active <= capacity` at every step, no user admitted twice and FIFO order preserved.
- Sweep frees expired admissions and admits the next buyer FIFO. Use a short-deadline test hook: deadlines come from constants passed via ARGV, so tests can use seconds instead of minutes.
- release: unknown id or expired admission → no-op. Mismatch → nothing changes.
- leave: waiting, prequeue, admitted, not-in-queue.
- Terminal status in config blocks join and admit. Admit still sweeps on a CLOSED event.
- cancel deletes everything. close keeps active admissions and the draining set.
- Two events admitting at once stay fully independent.

**Done when** all of the above pass reliably (run the concurrency test 20× in CI once).

**Status: code complete (2026-10-14); waiting for a real-Redis test run.**
- Built:
  - `app/queue/lua/{join,shuffle,admit,release,leave}.lua`
  - `app/queue/repository.py` (`QueueRepository`: script wrappers, `cancel_event`, `close_event`, `get_entry`, `counts`, `get_admission`, `list_admissions`, `is_shuffled`)
  - `app/queue/models.py` (result types and `Admission`, whose `to_public()` is the contract's admission object)
  - `app/queue/keys.py` (all key names)
- Tests: 58 in `tests/integration/redis/`:
  - `test_join`, `test_shuffle`, `test_admit`, `test_admit_concurrency`, `test_release`, `test_leave`, `test_cleanup`, `test_reads`, `test_multi_event`, plus the existing `test_server_time`
  - Fixtures in `tests/integration/redis/conftest.py`: `repo`, `short_repo` with 1 s / 2 s deadlines, `open_event`, `admitting_event`
- Pre-check: all 58 passed against fakeredis, which runs the real Lua scripts in-process, via a throwaway plugin in the scratch directory (not in the repo). It runs a different Lua version and has no true parallelism, so the real-Redis run is still required.
- Changed from the plan:
  - **Admission hashes store epoch milliseconds, not ISO strings.** Redis Lua has no date formatting (`os.date` is unavailable). Python formats ISO via `Admission.to_public()`, truncated to whole seconds.
  - **`join` checks an unexpired admission before the terminal status.** This implements the confirmed "admitted buyer during CLOSED gets their admission" rule atomically. An *expired*, unswept admission is cleaned up inline, so the buyer can rejoin at the back straight away.
  - **`release` has a fourth outcome, `expired`.** The deadline has passed but the sweep hasn't run: nothing changes and the sweep releases it. This matches "OrderConfirmed for an expired admission is a no-op".
  - **Event-mismatch detection.** `release` checks `queue:admission_index` first: if the admission belongs to a different event than the message says, it returns `mismatch` without touching either event.
  - **`cancel_event`/`close_event` don't write the status.** The caller must first move the status via `ConfigRepository.set_status`, which updates MySQL and the cache, so the scripts already refuse new work while cleanup runs.
  - **`shuffle` sorts the pre-queue before shuffling,** so a seeded `Random` in tests gives a repeatable order. Production uses `secrets.SystemRandom`.
  - **Safety TTLs never land in the past.** Scripts use `max(release_at + 3 days, now + 1 day)`, so an event released long ago can't have keys deleted on write.
  - **`list_admissions` breaks ties by id.** All admissions from one admit call share a score, so ties are resolved by `admission_id`, scanning past at most one tie group.
- Known limits:
  - `admit` generates `capacity` UUIDs per call, even when the pool is full. That's cheap at current sizes; trim it if profiling says so.
  - If the process dies between a script and the `admission_index` update, an index entry can be left behind. `get_admission` removes it lazily.

---

## Phase 3 – Admission tokens

**Steps**
1. `core/tokens.py`: load the private key and kid at startup, plus the public keys from `JWT_PUBLIC_KEYS_DIR`. `sign(admission) -> str` uses RS256 with header `{"alg","typ","kid"}`, and the claims are exactly the six admission fields (no `iat`/`exp`, since PyJWT adds none unless asked). `jwks() -> dict` returns `kty, kid, use, alg, n, e` (base64url) for the active key plus extras.

**Tests:** the same admission signed twice gives a byte-identical token. Verifying with the JWKS public key works. The claim set equals the admission exactly. The kid is in the header. Rotation: two keys in JWKS, sign with the new one.

**Status: done (2026-10-17).**
- Built:
  - `app/core/tokens.py`: `AdmissionTokens` with `from_settings`, `sign(admission)` and `jwks()`, plus key loading with clear `KeyConfigError` messages.
  - `tests/fakes/reservation_service.py`: `validate_admission_token`, Reservation's validation steps as a test double. Phase 8's E2E test will reuse it.
  - The Reservation-facing contract: [Admission token contract](Endpoint%20reference.md#admission-token-contract-for-reservation) in `docs/Endpoint reference.md`, linked from endpoints 3 and 9.
- Tests: 25 in `tests/unit/test_tokens.py`:
  - Token shape: the header is exactly `{alg, typ, kid}`, and the claims are exactly the six contract fields in a stable order.
  - Signing is deterministic: a fresh signer with the same key produces the identical token.
  - The JWKS has exactly the fields `kty, kid, use, alg, n, e`.
  - Rotation: the old and new keys are both published, and old and new tokens both validate.
  - Key files: a public-key file must match the active key, duplicate kids are refused, and missing, non-PEM, non-RSA and under-2048-bit keys are refused.
  - The Reservation checks: user mismatch, event mismatch, the selection deadline (to whole seconds), tampered claims, an unknown key or kid, algorithm confusion (`HS256` signed with the public key, and `none`), and garbage input.
- Changed from the plan:
  - Rotation files in `JWT_PUBLIC_KEYS_DIR` may be named `<kid>.pem` **or** `<kid>.public.pem`, matching what `make keys` writes, so a rotated-out key can be moved in as-is. The directory must hold public keys only.
  - A public file for the *active* kid is allowed (it's deduplicated) but must match the signing key.
  - Keys under 2048 bits are refused at startup.
- Not wired yet: the app lifespan (Phase 4) will build `AdmissionTokens.from_settings(...)` at startup, and the JWKS route lands in Phase 5.

---

## Phase 4 – Buyer API (`/api/v1`)

**Steps**
1. `api/deps.py`: `current_user` parses `X-User-Sub` as a UUID (otherwise 401 UNAUTHENTICATED) and returns the lowercase canonical string. This is the one place to swap in gateway JWT auth later. Also provides Redis, DB session and the script registry from app state.
2. `queue/service.py` orchestration:
   - **lobby(eid):** config → `{event_id, queue_status, queue_opens_at, release_at, server_time}`.
   - **join(eid, uid):** config → status. SCHEDULED → 409 QUEUE_NOT_OPEN with `details.queue_opens_at`. CLOSED/CANCELLED → check for an existing *unexpired* admission first; if found, return it with 200 (confirmed decision), otherwise 410 with the matching code. OPEN/ADMITTING → join script → 201 if created, else 200. A `closed` result from the script maps to 410, same as above.
   - **me(eid, uid):** builds the response per the CLAUDE.md lookup order and the Endpoint reference shapes. `release_at` only appears for OPEN waiting entries. `admission` + `admission_token` are only present when ADMITTED. `position` only appears for WAITING. While CLOSED or CANCELLED, waiting entries no longer exist, so they naturally return NONE.
   - **leave(eid, uid):** leave script → 204 / 404 NOT_IN_QUEUE / 409 ALREADY_ADMITTED. An unknown event → 404 EVENT_NOT_FOUND.
3. Response models in pydantic, using `response_model_exclude_none` only where the reference omits keys. **Careful:** `poll_after_seconds: null` must be present, not omitted, so build the response dicts explicitly per shape.
4. `api/buyer.py` routes. `app/main.py` lifespan creates and closes the pools and registers the scripts.

**Tests (API, httpx AsyncClient + real Redis/MySQL):** every status/error row of endpoints 1–4 in the Endpoint reference. Response shapes compared key-for-key against the reference examples. Repeated POST returns 200 with an identical token. Admitted buyer during CLOSED gets a 200 join, a new buyer during CLOSED gets 410. Missing or bad `X-User-Sub` → 401. Redis stopped → 503 body.

**Status: code complete (2026-10-17); integration tests waiting for a run.**
- Built:
  - `app/main.py`: `create_app(settings=None, *, rng=None)`. The lifespan loads the keys first, then creates the Redis client, MySQL engine and repositories, and closes them on shutdown.
  - `app/api/deps.py`: the `Services` container, the `current_user` dependency (`X-User-Sub` → canonical lowercase UUID) and the `QueueServiceDep` dependency.
  - `app/api/schemas.py`: response bodies and the documented error body.
  - `app/queue/service.py`: `QueueService` with `lobby`, `status`, `join` and `leave`.
  - `app/api/buyer.py`: the four routes.
- Tests:
  - `tests/unit/test_buyer_http.py`: 25 tests of the HTTP layer with the service stubbed out. Bodies are compared field for field, *and in order*, with the reference examples. Also covers 201 vs 200, 204 without a body, 401 for missing/empty/malformed `X-User-Sub` on all three routes, case-insensitive user and event ids, and malformed event ids → 404.
  - `tests/integration/api/test_buyer.py`: 19 tests against real Redis and MySQL. Covers the lifecycle (SCHEDULED 409, pre-queue, FIFO positions and poll intervals); the admitted buyer's admission and token, identical across polls and repeat joins and validated by the fake Reservation; rejoining at the back; sold out (admitted keep it, waiting see NONE, newcomers get 410); cancelled; a waiting entry reading NONE as soon as the status is terminal; leave (204/404/409); and unknown events → 404 on all four routes.
  - `tests/integration/api/test_errors_503.py`: Redis down (all four routes) and MySQL down → the 503 body, made unreachable by pointing the app at a closed port.
- Changed from the plan:
  - `release_at` is included whenever `position` is null, i.e. in the pre-queue. That also covers the window of under a second after release before the shuffle runs, not only while the queue is OPEN.
  - A WAITING entry is reported as NONE once the queue status is CLOSED/CANCELLED, even if the cleanup hasn't run yet, as the contract says.
  - The terminal-status check is atomic. `join` defers to the join script, so a sell-out landing mid-request still gives 410, and an admitted buyer still gets their admission.
  - The OpenAPI schema hides FastAPI's default 422 (this API returns 404/400 instead), so `/docs` lists only real responses.
- Order of checks on buyer routes: an invalid `X-User-Sub` gives 401 before the event lookup (404).

---

## Phase 5 – Internal API, JWKS, health

**Steps**
1. `GET /internal/v1/queues`: validate `status`, clamp `limit` to 1–200 (default 50), decode the cursor, run the keyset query and return `next_cursor`.
2. `GET /internal/v1/events/{eid}/queue`: MySQL row + effective status + live counts. `waiting_count` = ZCARD waiting + SCARD prequeue, so the OPEN pre-queue counts too.
3. `GET /internal/v1/events/{eid}/queue/admissions`: 404 if there is no config. Items are paged from the `active` ZSET (ascending), filtering out already-expired items.
4. `GET /internal/v1/admissions/{aid}`: index → hash. Missing or expired → 404 ADMISSION_NOT_FOUND. Never returns the token.
5. `GET /.well-known/jwks.json` with `Cache-Control: public, max-age=300`.
6. `GET /health/live` → `{"status":"ok"}`. `GET /health/ready` → `PING` Redis and `SELECT 1` MySQL with short timeouts → 200 `{"status":"ready"}` or 503 `{"status":"unavailable","failed":[...]}`.
7. Leave `/internal/v1` auth as a pluggable dependency that is a no-op for now (open point).

**Tests:** pagination across pages with no duplicates or gaps, the limit cap, status filter, counts, admission lookup after release → 404, the JWKS shape, and readiness with each dependency stopped.

**Status: done (2026-10-17).** All 133 integration tests passed on the compose test stack.
- Built:
  - `app/queue/admin.py`: `AdminService` (read-only): `list_queues`, `queue_detail`, `admissions`, `admission`, and `clamp_limit`.
  - `app/api/internal.py`: `router` for `/internal/v1` (endpoints 5–8, all behind the `internal_auth` dependency) and `ops_router` for JWKS and the two health checks.
  - New schemas in `app/api/schemas.py`.
  - `InvalidCursor` → 400 `INVALID_REQUEST` in `app/api/errors.py`.
- Tests:
  - `tests/unit/test_internal_http.py`: 20 tests with the service stubbed. Bodies match the reference field for field and in order. Also covers bad status/limit/cursor → 400, limit clamping, unknown or malformed ids → 404, an admission body without a token, JWKS shape and `Cache-Control`, liveness, and an OpenAPI check that all 11 spec endpoints exist.
  - `tests/integration/api/test_internal.py`: 12 tests. Paging with no gaps or duplicates (queues and admissions, including tied scores); the persisted-status filter vs. the effective status reported; the 200 cap; live counts including the pre-queue; `closed_at` after sell-out; admission lookup → 404 once released; and end to end, a token from `/queue/me` validated against the JWKS served over HTTP.
  - `tests/integration/api/test_health.py`: 5 tests. Ready with everything up, Redis down, MySQL down and both down, each naming the failed dependency; liveness unaffected. The down cases use closed ports, not mocks.
- Decisions:
  - **16 resolved:** a malformed cursor → 400 `INVALID_REQUEST` with `details.errors[0].loc = ["query", "cursor"]`. An unknown `status` or non-numeric `limit` gives the same 400.
  - `limit` outside 1–200 is **clamped**, not refused, as the plan said. Endpoints 5 and 7 in the endpoint reference now document this and the 400.
  - Readiness checks MySQL and Redis **concurrently**, each with a 1 s timeout. `failed` lists names in a fixed order (`mysql`, `redis`). The body is the plain health shape, not the error envelope, as the reference specifies.
  - JWKS and health have no auth dependency. `internal_auth` is a no-op hook on `/internal/v1` only, so the open point (network-only vs. a service token) can be settled in one place.

---

## Phase 6 – Admitter worker

**Steps**
1. `workers/admitter.py`: async loop per CLAUDE.md, tick = `ADMITTER_TICK_SECONDS`, graceful SIGTERM handling (finish the current tick, close pools).
2. Each tick:
   1. **Promote:** `config_repo.due_for_open(now)` → persist OPEN, refresh cache, `SADD live_events` (decision 3). Events whose `release_at` is already past also get promoted, so the next step handles them.
   2. **For each live event** (failures isolated per event):
      - load config. `effective_status` terminal → `SREM live_events` and skip (defensive).
      - `now < release_at` → nothing.
      - not `shuffled` → `SMEMBERS prequeue`, shuffle in Python, run the shuffle script, persist ADMITTING to MySQL. Persist ADMITTING even if the shuffle aborted because another run already did it, since persisting is idempotent.
      - run admit with `capacity` UUIDs → update `admission_index` → log released/admitted (Phase 9 publishes them).
   3. **For each draining event:** run admit (sweep-only, because the status is terminal) and `SREM draining_events` when `active` is empty.
3. Write the heartbeat `SET queue:admitter:heartbeat <ts> PX 10000` on every tick.
4. Warn when a tick takes longer than 0.8 s (signal to switch to `asyncio.gather`).

**Tests (integration):** run `process_event` directly with a controllable clock, or offset `release_at` relative to Redis TIME. SCHEDULED→OPEN promotion, shuffle at release, fill to capacity, expiry sweep refills FIFO, draining cleanup, and one failing event doesn't stop the others.

**Status: done (2026-10-17).** All 144 integration tests passed on the compose test stack.
- Built:
  - `app/queue/admitter.py`: `Admitter` with `tick()`, which returns a `TickReport` (opened, shuffled, admitted, released, dropped_live, drained, failed), and `run(stop)`.
  - `workers/admitter.py`: the entrypoint (`make admitter`). It handles SIGTERM/SIGINT and closes its connections.
  - New helpers:
    - `ConfigRepository.live_or_due(now)`
    - `QueueRepository`: `pool_size`, `live_event_ids`, `add_live`, `remove_live`, `draining_event_ids`, `remove_draining`
    - `keys.ADMITTER_HEARTBEAT`
- Tests:
  - `tests/unit/test_admitter_loop.py`: 5 tests. Pacing; stop lets the current tick finish without waiting out the interval; a failing tick doesn't stop the loop; slow-tick warning; per-event isolation.
  - `tests/integration/workers/test_admitter.py`: 10 tests. Promotion (due vs. not due); rebuilding `live_events` from MySQL; nothing admitted before release; shuffle at release with the seeded order checked buyer by buyer, pool filled, ADMITTING persisted and no second shuffle; catching up an event released while the admitter was down; expiry sweep refilling FIFO; a terminal event leaving the work list; draining until empty; a corrupt config isolated; the heartbeat.
  - `tests/integration/workers/test_admitter_entrypoint.py`: SIGTERM stops the real entrypoint while every tick fails. It uses closed ports, so it needs no services.
- Changed from the plan:
  - **One query instead of two.** `live_or_due` returns SCHEDULED-and-due *and* all OPEN/ADMITTING rows, and the admitter re-adds them all to `queue:live_events` every tick. The work list therefore heals itself if Redis loses it, at the cost of one indexed query per second.
  - **ADMITTING is persisted** whenever the cached config isn't already ADMITTING, not only right after a shuffle. That covers a crash between the shuffle and the status write. `set_status` refuses it if the event went terminal in the meantime.
  - **The heartbeat holds Redis server time in ms**, with a 10 s TTL. Phase 8's health check reads it.
  - Tick-level failures (Redis or MySQL down) are logged and retried on the next tick. They never end the process.

---

## Phase 6b – Sale end (`sale_ends_at`)

Decided on 2026-10-17 (see the confirmed-decisions table). It's built before Phase 7 because the consumer must already accept the new field.

**Steps**
1. **Migration `0002`:** add a nullable `sale_ends_at DATETIME(3)` column to `queue_configs`. Add it to the model, `QueueConfigData` and the Redis config cache (ms, empty for null).
2. **`effective_status`:** after the terminal check and before the time-derived states, `sale_ends_at is not None and now >= sale_ends_at` → CLOSED.
3. **Telling the two endings apart:** a new `close_reason(cfg, now)` returns `SALE_ENDED` when `sale_ends_at` ≤ (`closed_at` or `now`), else `SOLD_OUT`. Add a `SaleEnded` error (410 `SALE_ENDED`). `QueueService.join` uses `close_reason` to pick between the 410s. An admitted buyer still gets 200 with their admission.
4. **Responses:** add `sale_ends_at` (ISO or null) to the lobby response (endpoint 1) and the internal detail (endpoint 6).
5. **Admitter:** for a live event whose effective status became CLOSED *because the sale ended* while the persisted status isn't terminal: `set_status(CLOSED, closed_at=sale_ends_at)`, then `close_event` (waiting cleared; the pool drains as for a sell-out). Add it to `TickReport.closed`. This happens before any admit in that tick, so nobody is admitted after the end.
6. **Phase 7 groundwork:** the payload schemas take `sale_ends_at: datetime | None = None` and reject `sale_ends_at <= release_at` as malformed.

**Tests:**
- Unit: `effective_status` and `close_reason` at the boundaries (just before and at `sale_ends_at`; sold out before the end; sale ended with no `closed_at` yet).
- Integration:
  - The migration adds the column, `alembic check` stays clean, and the cache round-trips the field.
  - Joining after the end gives 410 `SALE_ENDED`; joining after a sell-out still gives `EVENT_SOLD_OUT`; an admitted buyer still gets 200.
  - The lobby shows `sale_ends_at`.
  - An admitter tick at the sale end persists CLOSED with `closed_at = sale_ends_at`, clears the waiting queue, keeps admitted buyers and drains the pool.
  - An event with no `sale_ends_at` never closes on its own.

**Status: done (2026-10-17).** All 151 integration tests passed on the compose test stack.
- Built:
  - Migration `migrations/versions/0002_sale_ends_at.py`.
  - `sale_ends_at` on the `QueueConfig` model, `QueueConfigData` and the Redis cache. Cache entries written before this change simply read as `None`.
  - In `app/core/status.py`: the sale-end rule in `effective_status`, plus the new `CloseReason` and `close_reason()`.
  - `SaleEnded` (410 `SALE_ENDED`) in `app/api/errors.py`.
  - `sale_ends_at` in the lobby and internal detail responses.
  - `Admitter._close_for_sale_end` and `TickReport.closed`.
- Changed from the plan:
  - **Joining a closed queue is now read-only.** `QueueService.join` no longer calls the join script once the effective status is terminal. It reads the buyer's entry: an admitted buyer gets 200 with the admission; anyone else gets the 410. So in the ≤1 s between the sale's end and the admitter persisting CLOSED, a join can't sneak an entry into the waiting queue. Behaviour for sold-out/cancelled is unchanged (the existing tests still apply).
  - **No promotion for an event whose sale has already ended.** The admitter doesn't move a SCHEDULED event to OPEN if its sale is already over (e.g. the admitter was down). It goes straight to CLOSED on that tick.
  - **Step 6 (payload schemas) moved into Phase 7,** where `messaging/schemas.py` is created. The rule stays: optional `sale_ends_at`, and `sale_ends_at <= release_at` → malformed.
- Tests:
  - Unit (11 new): sale-end boundaries in `effective_status` (just before / at / long after); a stored terminal status wins; no end means never closing; five `close_reason` cases; the `SALE_ENDED` error body.
  - Integration (7 new):
    - API: joining after the end gives 410 `SALE_ENDED` and writes nothing; an admitted buyer keeps the admission after the end; a sell-out before the end is still `EVENT_SOLD_OUT`; the lobby shows `sale_ends_at`.
    - Admitter: a tick at the end persists CLOSED with `closed_at = sale_ends_at`, clears the waiting queue, keeps the admitted buyer and drains; an event whose sale ended while the admitter was down goes straight to CLOSED; a future end keeps admitting.
  - 5 existing tests updated for the new field: the lobby/detail bodies in unit and integration, `Cfg` in `test_status`, the cache round trip, and the migration column check.

---

## Phase 7 – RabbitMQ consumer

**Steps**
1. `messaging/topology.py`: idempotent declarations.
   - Exchanges (topic, durable): `event.events`, `reservation.events`, `checkout.events`, `queue.events`. DLX `queue-service.dlx` (direct, durable).
   - Queues (`x-queue-type: quorum`, durable, `x-dead-letter-exchange: queue-service.dlx`, `x-dead-letter-routing-key: <queue>.dlq`, `x-delivery-limit: 5`): the three consumer queues from CLAUDE.md, plus the three `.dlq` quorum queues bound to the DLX.
   - Bindings as in the table (`event.*`, `event.sold_out`, `order.confirmed`).
2. `messaging/schemas.py`: pydantic `Envelope` and one model per payload (UUIDs, aware datetimes, `total_capacity >= 0`, `event_version >= 1`, optional `sale_ends_at` > `release_at`). Unknown `message_type` → poison.
3. `messaging/handlers.py`: pure async handlers. Each takes the session, Redis and the parsed message and contains no pika code, so each is unit-testable.
   - **EventPublished:** `SELECT … FOR UPDATE`. If the row exists with version ≥ incoming → ignore. Else insert as SCHEDULED with derived fields + `processed_messages` → commit → write the cache.
   - **EventUpdated:** lock the row. Missing → upsert as Published (decision 8). Version ≤ stored → ignore. Compute `effective_status` from stored values and server time. SCHEDULED → update fields, recompute derived ones, bump the version → commit → refresh the cache. OPEN/ADMITTING → log `event_update_rejected` with old/new values and leave the row unchanged, still recording `message_id`. Terminal → ignore.
   - **EventCancelled:** lock the row. Version ≤ stored *and* already CANCELLED → re-run Redis cleanup only (decision 6). Otherwise set CANCELLED, `closed_at = cancelled_at`, store the version → commit → `cancel_event(eid)`.
   - **EventSoldOut:** lock the row. Already CANCELLED → ignore. Already CLOSED → re-run `close_event` (idempotent) and ack. Else set CLOSED, `closed_at = sold_out_at` → commit → `close_event(eid)`.
   - **OrderConfirmed:** Redis only. Run the release script: not found → info log; mismatch → error log `admission_integrity_mismatch` with both values; released → `HDEL admission_index` and, if `effective_status == ADMITTING`, run admit for that event right away (same function as the admitter). The `message_id` is still recorded in `processed_messages` as a fast-path dedupe.
4. `messaging/consumers.py` + `workers/consumer.py`: one thread per queue, each with its own `BlockingConnection` and channel, `basic_qos(prefetch_count=50)` and manual ack. pika is blocking, so each thread owns its own asyncio event loop with its own Redis/DB clients and runs handlers through `loop.run_until_complete`. The callback does: parse (poison → nack, no requeue) → dedupe check → handler → ack, or nack with requeue on a transient error. Reconnect with backoff on connection loss. Shut down cleanly on SIGTERM. Heartbeat key `queue:consumer:heartbeat`.

**Tests:**
- Handler tests (real MySQL + Redis): every rule in the CLAUDE.md "Consumed messages" section, including stale versions ignored, EventUpdated rejected while OPEN/ADMITTING (row unchanged), SoldOut keeps active admissions and clears waiting, Cancelled clears everything, OrderConfirmed admits the next buyer immediately, unknown or expired admissions are a no-op, mismatch changes nothing, and duplicate `message_id` is a no-op.
- Topology/consumer test (real RabbitMQ from the test stack): declare twice without error, `FakeEventService`/`FakeReservationService`/`FakeCheckoutService` publish one of each message → effects observed, a poison message lands in the DLQ, a handler that always fails dead-letters after 5 deliveries.

**Status: done (2026-10-17).** All 187 integration tests passed on the compose test stack.
- Built:
  - `app/messaging/schemas.py`: the envelope and five payloads, and `parse_message` → `Message` or `PoisonMessage`. UUIDs are canonicalised, datetimes must carry an offset and are converted to UTC, unknown fields are ignored, and `sale_ends_at` is optional but must be later than `release_at` (Phase 6b step 6).
  - `app/messaging/topology.py`: exchanges, DLX, quorum queues with DLX/DLQ and the delivery limit, and bindings.
  - `app/messaging/handlers.py`: `MessageHandlers.handle`, which returns a `HandleResult` (an `Outcome` plus, for OrderConfirmed, how many buyers were admitted at once).
  - `app/messaging/consumers.py`: `dispose()` (the ack / requeue / dead-letter decision), `QueueConsumer` (one thread per queue), `declare_with_retry` and `start_consumers`.
  - `workers/consumer.py`: the entrypoint (`make consumer`).
  - `keys.CONSUMER_HEARTBEAT_PREFIX`: one heartbeat key per queue, `queue:consumer:heartbeat:<queue>`.
- Fakes: `tests/fakes/messages.py` (builders for every consumed message, using the real envelope, exchange and routing key), `publisher.py` (persistent messages, publisher confirms, `mandatory`), `FakeEventService`, `FakeCheckoutService`, and `FakeReservationService` (now also publishes).
- Tests:
  - Unit, 27 new:
    - `test_schemas.py`: every message parses, plus 14 poison cases.
    - `test_dispose.py`: ack, dead-letter and requeue.
    - `test_topology.py`: exact declarations, using a recording channel.
  - Integration, 36 new:
    - `messaging/test_handlers.py` (18): every rule in "Consumed messages". Duplicates, stale versions, an upsert for an unknown event, rejection while OPEN/ADMITTING, the cancel cleanup re-applied on redelivery, sold out then cancelled, an immediate admit on OrderConfirmed, mismatch on user or event, an expired admission, and no admit after a sell-out.
    - `messaging/test_topology.py` (11): declaring twice; each routing key reaches its queue; an unrelated key is unroutable; rejects go to the right DLQ; the attempt count.
    - `messaging/test_consumer.py` (6): real consumer threads fed by the fakes; poison to the DLQ; a handler that always fails is retried `MAX_ATTEMPTS` times, then dead-lettered; the heartbeat.
    - `workers/test_consumer_entrypoint.py` (1): SIGTERM while the broker is down gives a clean exit (subprocess, no services needed).
- Decisions:
  - **`processed_messages` is a record, not a skip switch.** The `message_id` is inserted (`INSERT IGNORE`) in the same transaction as the state change. A duplicate still re-runs the idempotent Redis side effects, so a Redis failure after the MySQL commit is repaired on redelivery (decision 6). Cancel/sold-out duplicates return `REAPPLIED`.
  - **`x-delivery-limit` is 4**, which means **5 attempts in total** (CLAUDE.md: "dead-letter after 5 failed attempts"). RabbitMQ counts *redeliveries*. **Confirmed against the real broker** by `test_failing_message_is_dead_lettered_after_max_attempts` and `test_failing_handler_is_retried_then_dead_lettered`.
  - **EventUpdated rejected while OPEN/ADMITTING:** the row is untouched, *including* `event_version`. The rejection is logged as `event_update_rejected` with the stored and incoming values.
  - **EventCancelled with a stale version** (≤ stored, not yet cancelled) is ignored and logged. A cancel for an already-CANCELLED event re-applies the cleanup.
  - **EventSoldOut for an event already CLOSED** (sold out, or the sale ended) re-applies `close_event`. For a CANCELLED event it's ignored.
  - **OrderConfirmed** is Redis-only, plus recording the `message_id`. It admits the next buyer only if the effective status is ADMITTING, so a slot freed after a sell-out or sale end stays empty.
  - **Each consumer thread owns its own** asyncio loop, Redis client and MySQL engine (asyncmy pools are bound to a loop). The thread polls `process_data_events(time_limit=1)` rather than `start_consuming`, so stopping and heartbeats need no cross-thread callbacks.
  - **A crashed consumer thread stops the whole process (exit 1),** so the orchestrator restarts it rather than leaving a queue unconsumed.
- Test fix during verification: the first version of the max-attempts test failed. Quorum queues apply a `nack(requeue=True)` asynchronously, so an empty `basic_get` straight after a nack was misread as "dead-lettered". The test now polls the queue and its DLQ until the message appears in one of them. The production code didn't change.
- Known gaps (left for Phase 8):
  - ~~The plain log format doesn't print the `extra` fields~~ → done in Phase 8 (`app/core/logging.py`).
  - ~~`processed_messages` grows forever~~ → done in Phase 8 (`workers/prune.py` + index migration `0003`). **Superseded 2026-10-21:** the table and `workers/prune.py` were removed (migration `0004`).

---

## Phase 8 – End-to-end, load and operations

**Steps**
1. **E2E scenario test** (`tests/e2e/test_full_flow.py`: API, admitter and consumer run in-process against the test stack with short deadlines. The other microservices are played by `tests/fakes/`, and `FakeReservationService.validate_token` checks every issued token):
   EventPublished → buyers join during OPEN (pre-queue) → release → shuffle → pool filled → poll returns token → token verifies against JWKS → OrderConfirmed → next buyer admitted → deadline expiry → refill → EventSoldOut → waiting buyers see NONE/CLOSED, admitted buyers keep their admission → later EventCancelled on a second event clears it.
2. **Load smoke** (locust or a simple asyncio script): 20k joins in the OPEN window plus polling at the server-directed intervals. Check Redis CPU, API p99 for `/queue/me`, and admitter tick duration with 10 concurrent live events.
3. **Packaging:** one Dockerfile with three entrypoints (`uvicorn app.main:app`, `python -m workers.admitter`, `python -m workers.consumer`). Compose services for all three. Document that the admitter must run with `replicas: 1`. Migrations run as a separate one-off command before the services start.
4. **Observability:** structured JSON logs with `event_id`, `admission_id` and `message_id` fields. Optional Prometheus metrics: admitted/released counters by reason, waiting size, active size, tick duration and consumer handler latency.
5. **Worker health:** container healthchecks for the admitter and consumer read their heartbeat key (`python -m workers.healthcheck admitter`).
6. **README/runbook:** running locally, key generation, key rotation procedure (add new kid → switch the signing key → remove the old public key after 11 minutes), replaying from a DLQ.

**Status: code complete (2026-10-17); waiting for your test run, the Docker build and a load smoke run.**
- Built:
  - **E2E** (`tests/e2e/`): `conftest.py` runs the real API app (with its lifespan), the admitter loop (0.2 s ticks) and three real consumer threads, with selection 3 s / admission 6 s, all driven only by the fakes. `test_full_flow.py` has two scenarios:
    - **Full lifecycle:** EventPublished → lobby OPEN → 5 buyers in the pre-queue → release → pool of 2 filled, 3 waiting at positions 1–3, ADMITTING persisted → tokens validated against the served JWKS → late joiner at position 4 → OrderConfirmed admits the next buyer at once → a deadline expiry refills the pool → EventSoldOut: holders keep their admission, everyone else NONE, newcomers 410, an admitted rejoin gets 200 → internal detail CLOSED with `waiting_count` 0.
    - **Cancellation:** EventCancelled ends admissions and gives 410 `EVENT_CANCELLED`.
  - **Load smoke** (`scripts/load_smoke.py`): publishes an event via RabbitMQ; N buyers join and poll at the server-directed intervals; admitted buyers "check out" after 1–3 s via OrderConfirmed (`--no-orders` exercises the sweep instead). Reports nearest-rank p50/p95/p99 per endpoint, status codes, transport errors and admitted/ordered counts.
  - **Packaging:**
    - `Dockerfile`: multi-stage uv build, slim runtime, non-root uid 10001, `LOG_FORMAT=json`. Its default command runs the API; admitter, consumer and migrate are chosen by command.
    - `.dockerignore`: keeps secrets, tests and docs out of the image.
    - `docker-compose.yml` profile `app`: migrate (one-off), then api (port 8000), admitter (`replicas: 1`) and consumer, each with a healthcheck and `restart: unless-stopped`. The keys are mounted read-only from `./secrets`.
    - Make target `up-app` (and `down` now also stops the app containers).
  - **Observability:** `app/core/logging.py` has a JSON formatter (one object per line, `extra` fields included) and a text formatter (`key=value` suffix). New `LOG_FORMAT` setting (json|text). The admitter, consumer and API (production path) all use it; uvicorn's logs go through the same handler. Prometheus metrics: **not done**, still optional.
  - **Worker health:** `workers/healthcheck.py` (`python -m workers.healthcheck admitter|consumer`) checks for missing, stale or unreadable heartbeats. The admitter must have beaten within 10 s; the consumer needs all three queue heartbeats within 30 s. Exits 0 or 1.
  - **Retention:** `workers/prune.py` deletes `processed_messages` older than N days (default 30) in batches of 5,000 by MySQL's `UTC_TIMESTAMP`. It refuses windows under a day. Migration `0003` adds `idx_processed_at`.
  - **DLQ tooling:** `workers/dlq.py` offers `list`, `peek` (non-destructive) and `replay`. Replay sends messages to Queue's own consumer queue via the default exchange, keeps `message_id`/`type`, drops the broker's `x-death`/`x-delivery-count` headers, and acks the DLQ copy only after a publisher confirm.
  - **Runbook:** an Operations section in `README.md` (tracked by git, unlike `docs/`): processes and replicas, deploy order, health, log messages to watch, dead letters, retention, key rotation, load smoke.
- Tests:
  - Unit, 15 new: `test_logging.py` (5), `test_healthcheck.py` (3), `test_load_smoke.py` (7).
  - Integration/e2e, 9 new: `e2e/test_full_flow.py` (2), `workers/test_healthcheck.py` (2), `workers/test_prune.py` (2), `messaging/test_dlq.py` (3). `test_migrations.py` also checks the new index.
- Notes:
  - E2E review fixes before handing over: assertions after the sell-out check the rule (holders keep their admission, everyone else is out) rather than specific buyers, because deadlines race the sold-out message. The cancellation scenario's capacity was corrected to 10 (pool of 2).
  - The fakes now enable publisher confirms once per channel (pika logs an error on a repeat call).
  - **Fixes after the first run (2026-10-17):**
    - `test_full_lifecycle`: the final rejoin used the *earliest* admission holder, whose deadline could land between the snapshot and the rejoin, so it correctly got 410 and the test hit a `KeyError`. It now rejoins as the most recently admitted holder and checks the same `admission_id` comes back.
    - `test_list_peek_and_replay`: `peek` requeues what it reads, and on quorum queues a requeued message goes to the *back*, so peeking reorders the DLQ. The tool's behaviour is fine (event handlers are version-guarded), so it's now documented in `peek` and the runbook. The test is split: peek leaves messages in place; replay (without a prior peek) moves the oldest first and strips the broker headers.
  - In compose, the API container reads `./secrets/jwt_private.pem`, which is `0600`. On Docker Desktop for Mac that's fine. On Linux the container's uid 10001 may be refused; the README says to `chmod 644` **the local dev key only**.

---

## Phase 9 – Optional: publish AdmissionGranted / AdmissionReleased

Do this after the core is live. It changes no existing contract.

1. `messaging/publisher.py`: a blocking pika publisher with confirms and persistent messages. The admitter runs it in a thread executor.
2. Emit `AdmissionGranted` for every admitted entry, plus `AdmissionReleased` with reason ORDER_CONFIRMED (consumer), ADMISSION_EXPIRED (sweep) or EVENT_CANCELLED (cleanup collects the ids before deleting).
3. Publish failures must never block admission. Log and continue at first. Add a Redis-list outbox if delivery guarantees become a requirement.

---

## Phase 10 – Deploy to AWS

Start this only once Phases 0–8 are green against containers. Rewritten 2026-10-20 for the actual target (the earlier draft assumed ElastiCache and Amazon MQ).

### Target

| Part | Where | Who runs it |
| --- | --- | --- |
| RabbitMQ | **Centralised broker** shared by all services | Platform/broker team. We get a vhost and a user; we don't configure the broker. |
| MySQL | **Shared RDS instance**, our own database `queue_service` inside it | RDS owners run the instance and parameter group; we own the schema (Alembic). |
| Redis | **Our own EC2 instance** | Us: install, config, patching, backups, monitoring. |
| api | ECS service behind a load balancer, 2+ tasks | Us |
| admitter | ECS service, **exactly 1 task**, no load balancer | Us |
| consumer | ECS service, 1–2 tasks, no load balancer | Us |
| migrate, dlq | One-off ECS tasks from the same image | Us (CI/CD or an operator) |

Everything runs from the **one image** built by the `Dockerfile`; each ECS task definition only changes the command. Launch type: **Fargate** (no EC2 hosts to manage for the app). Not Lambda: the admitter ticks every second (EventBridge's minimum is 1 minute) and the consumer holds long-lived AMQP connections. Not on the Redis EC2 box: that would tie app deploys and crashes to the instance that holds all runtime state.

```
                 buyers ──► API gateway / public ALB ──► api (ECS, N tasks) ──┐
 Reservation ──► internal ALB (/internal/v1, /.well-known/jwks.json) ──► api  │
                                                                              ├──► Redis (EC2, ours)
                                     admitter (ECS, 1 task) ──────────────────┤
 central RabbitMQ ◄──────────────►  consumer (ECS, 1–2 tasks) ────────────────┤
                                     migrate / dlq (one-off tasks) ─────────┴──► shared RDS: queue_service DB
```

### 10.0 Decisions to confirm before provisioning

Ask these up front; several of them change the steps below.

- [ ] **Broker team:** RabbitMQ version (must be **3.13+** for quorum queues and `x-delivery-limit`), cluster size, AMQPS endpoint (port 5671) and its CA (public or private), vhost name per environment, and whether **we** may declare our own queues/DLX or **they** create everything in IaC (decides `RABBITMQ_TOPOLOGY`, see 10.1). Also who owns the `event.events`, `reservation.events` and `checkout.events` exchanges (the owning services, not us) and how we see DLQ depth (their monitoring, or the management API).
- [ ] **RDS owners:** instance endpoint, the database and users they'll create for us (or the admin access to create them), the `max_connections` budget we may use, whether `require_secure_transport` is on, and the global `time_zone` (should be UTC).
- [ ] **Network:** which VPC and private subnets the ECS tasks run in; whether RDS and the broker are in the same VPC or reached through peering/Transit Gateway.
- [ ] **Edge:** what fronts the buyer API. `X-User-Sub` must only ever be set by a trusted gateway, never accepted straight from the internet. `/internal/v1/*` must not be public (auth for it is still an open point).
- [ ] **Redis availability:** a single EC2 instance is a single point of failure: if it goes down the waiting room is down until it's back, and it loses up to ~1 s of writes (AOF `everysec`). Accept this for go-live, or plan a replica + Sentinel (code change 10.1 F).
- [ ] **Environments and naming:** e.g. `staging` and `prod`, AWS region, account(s).

### 10.1 Code changes (do first; testable locally)

All of these are small and covered by unit tests plus the compose test stack. Local defaults keep today's behaviour, so `docker-compose.yml` and the test stack keep working unchanged.

- **A. RabbitMQ topology modes** — **done differently, 2026-10-21:** a boolean `RABBITMQ_DECLARE_TOPOLOGY` (default true). `false` declares nothing and only passively checks our three queues (`check_queues`); the broker team creates the queues, DLQs and bindings. See `docs/deployment.md` A10. Original proposal, kept for reference: (`app/messaging/topology.py`). New setting `RABBITMQ_TOPOLOGY`:
  - `all` (default, local/tests): today's behaviour, declare all four exchanges.
  - `own` (shared broker, we may configure our own resources): declare only Queue's resources (`queue.events`, `queue-service.dlx`, the three consumer queues, their DLQs, their bindings) and check the other services' exchanges with `passive=True`, failing fast with a clear log if one is missing. Re-declaring someone else's exchange with different arguments would close the channel with `PRECONDITION_FAILED` and crash-loop the consumer.
  - `check` (broker team owns everything in IaC): passively check every exchange and queue; declare nothing.
- **B. JWT keys from env** (`app/core/config.py`, `app/core/tokens.py`). ECS injects secrets as env vars, not files. Add `JWT_PRIVATE_KEY` (PEM content) as an alternative to `JWT_PRIVATE_KEY_PATH`, and `JWT_PUBLIC_KEYS` (JSON `{kid: pem}`) as an alternative to `JWT_PUBLIC_KEYS_DIR` for rotation. Make the JWT settings optional for processes that don't sign (admitter, consumer, migrate, prune, dlq), and required by the API at startup.
- **C. MySQL TLS and time zone** (`app/core/db.py`, `migrations/env.py`). Add `MYSQL_SSL_CA` (path to the RDS CA bundle, which is baked into the image since it's public) and pass an `ssl.SSLContext` through `connect_args`. Run `SET time_zone = '+00:00'` on each new connection so `CURRENT_TIMESTAMP(3)` defaults are UTC even if the shared parameter group isn't.
- **D. MySQL pool size** (`app/core/db.py`). Add `MYSQL_POOL_SIZE` (default 5) and `MYSQL_MAX_OVERFLOW` (default 10) so the connection total fits the shared instance's budget (see 10.4).
- **E. RabbitMQ TLS.** `amqps://` already works: pika's `URLParameters` uses `ssl.create_default_context()`, which verifies against the system CAs. If the broker uses a **private CA**, add `RABBITMQ_SSL_CA` and build the `SSLOptions` from it.
- **F. (Only if decided in 10.0) Redis Sentinel.** Add `REDIS_SENTINELS` + `REDIS_SENTINEL_MASTER` so `create_redis` uses `redis.asyncio.Sentinel`. `register_script` already handles `NOSCRIPT` after a failover.
- **G. Docs.** Add the new variables to `.env.example` and the README runbook.

Redis TLS needs no code: `rediss://` with a password already works through `Redis.from_url`. `/health/ready` already has a per-check timeout.

**Done when** unit tests, lint and mypy pass, and the compose test stack still passes with the defaults.

### 10.2 AWS foundations

Do this once per environment (staging first). Use IaC (Terraform/CDK/CloudFormation, whichever the platform uses) so prod is a copy of staging.

1. **ECR repository** `queue-service` with immutable tags and scan-on-push.
2. **Security groups:**
   - `sg-queue-api-lb`: inbound 443 from the gateway / internal callers.
   - `sg-queue-app` (all ECS tasks): inbound 8000 from `sg-queue-api-lb` only; outbound to Redis, RDS, the broker and AWS endpoints.
   - `sg-queue-redis`: inbound 6379 **only** from `sg-queue-app` (and an ops bastion/SSM if needed). Nothing else.
   - Ask the RDS owners to allow `sg-queue-app` on 3306, and the broker team to allow it (or our NAT IPs) on 5671.
3. **Outbound for tasks in private subnets:** a NAT gateway, or VPC endpoints for ECR (`ecr.api`, `ecr.dkr`, S3 gateway), Secrets Manager, CloudWatch Logs, and SSM messages (for ECS Exec).
4. **IAM:**
   - Task **execution** role: pull from ECR, read the `queue-service/<env>/*` secrets (plus `kms:Decrypt` if they use a customer key), write to the log groups.
   - Task role: none needed by the app (it calls no AWS APIs). Add `ssmmessages:*` channel permissions only if ECS Exec is enabled.
   - CI role: push to ECR, register task definitions, `ecs:RunTask`/`UpdateService`, `iam:PassRole` for the two roles above.
5. **CloudWatch log groups:** `/ecs/queue-service/<env>/{api,admitter,consumer,migrate,ops}`, with a retention period (e.g. 30 days).
6. **Private DNS** (Route 53 private hosted zone), e.g. `redis.queue.<env>.internal` pointing at the Redis instance, so `REDIS_URL` never contains an IP.

### 10.3 Secrets

Store one Secrets Manager secret per value under `queue-service/<env>/`:

| Secret | Value | Used by |
| --- | --- | --- |
| `mysql-url` | `mysql+asyncmy://queue_app:<pw>@<rds-endpoint>:3306/queue_service` | api, admitter, consumer |
| `mysql-migrate-url` | same with the `queue_migrate` user | migrate (injected **as** `MYSQL_URL`) |
| `redis-url` | `redis://queue:<pw>@redis.queue.<env>.internal:6379/0` (`rediss://` if TLS is on) | all |
| `rabbitmq-url` | `amqps://<user>:<pw>@<broker-host>:5671/<vhost>` (vhost URL-encoded, `/` → `%2F`) | consumer, dlq (and api/admitter until settings make it optional) |
| `jwt-private-key` | PEM from `scripts/gen_jwt_key.py`, generated locally and never committed | api |

1. Generate a **separate key per environment** with `uv run python -m scripts.gen_jwt_key`, with kids such as `queue-staging-2026-10` and `queue-prod-2026-10`. Put the private PEM into the secret, then delete the local copy.
2. Plain (non-secret) env vars go straight into the task definitions: `JWT_KID`, `ADMITTER_TICK_SECONDS=1`, `LOG_LEVEL=INFO`, `LOG_FORMAT=json`, `MYSQL_SSL_CA`, `RABBITMQ_TOPOLOGY`, pool sizes.
3. Ask the RDS owners and the broker team to put the passwords they create into these secrets (or hand them over through a secure channel), so the passwords never pass through chat or tickets.

### 10.4 MySQL: our database on the shared RDS instance

1. Create the database and two users (or have the RDS owners run this):

   ```sql
   CREATE DATABASE queue_service CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;

   CREATE USER 'queue_app'@'%' IDENTIFIED BY '<pw>' REQUIRE SSL;
   GRANT SELECT, INSERT, UPDATE, DELETE ON queue_service.* TO 'queue_app'@'%';

   CREATE USER 'queue_migrate'@'%' IDENTIFIED BY '<pw>' REQUIRE SSL;
   GRANT ALL PRIVILEGES ON queue_service.* TO 'queue_migrate'@'%';
   ```

   The app user can't change the schema; only the migrate task can.
2. Check the time settings once:
   `SELECT @@global.time_zone, @@session.time_zone, NOW(3), UTC_TIMESTAMP(3);`.
   The two times must match. Code change 10.1 C protects us either way.
3. **Connection budget.** Each process holds up to `MYSQL_POOL_SIZE + MYSQL_MAX_OVERFLOW` connections. The total is (max api tasks × that) + admitter + consumer tasks + 1 migrate/prune, and it must stay inside the share the RDS owners give us. Example: budget 100, api autoscaling up to 6 tasks → pool 8 + overflow 4 = 12 per task → 72, plus ~3 × 12 for the workers ≈ 108, which is too many. Shrink the worker pools (they need ~2–3 each) or the overflow.
4. Put the RDS CA bundle (`global-bundle.pem` from AWS) into the image, e.g. `/app/certs/rds-global-bundle.pem`, and set `MYSQL_SSL_CA` to it.
5. The schema is created by the migrate task in 10.8; never by hand.

### 10.5 Redis on our own EC2 instance

1. **Instance:** a memory-optimised, **non-burstable** type (e.g. `r7g.large`, 2 vCPU / 16 GB; Lua scripts are CPU-bound and run single-threaded, so burst credits would bite on release spikes). Use a private subnet, `sg-queue-redis`, IMDSv2 only, and **SSM Session Manager instead of SSH** (no key pair, no port 22). Use the current Amazon Linux 2023 AMI. Size from the load smoke: queue state is small (roughly a few hundred bytes per waiting buyer or admission), so memory is mostly headroom.
2. **Storage:** a separate encrypted gp3 EBS volume mounted at `/data` for the AOF. Daily snapshots via Data Lifecycle Manager.
3. **Install:** run the **same `redis:7.4` image the tests use**, under Docker with a systemd unit (`--network host`, `/data` and `/etc/redis` mounted, `restart=always`), or install Redis 7.4 from a package. Pin the version either way.
4. **OS tuning:** `vm.overcommit_memory=1`, transparent huge pages off, `net.core.somaxconn=1024`. Leave chrony on Amazon Time Sync: **all queue timing comes from Redis `TIME`**, so this clock is the service's clock.
5. **`/etc/redis/redis.conf`** (essentials):

   ```
   port 6379
   protected-mode yes
   dir /data
   appendonly yes
   appendfsync everysec
   maxmemory 12gb                  # ~75% of RAM
   maxmemory-policy noeviction     # REQUIRED: eviction would silently drop queue/admission keys
   aclfile /etc/redis/users.acl
   ```

   With `noeviction`, a full Redis rejects writes loudly (joins fail and readiness goes 503) instead of losing buyers' places.
6. **`/etc/redis/users.acl`:**

   ```
   user default off
   user queue on >APP_PASSWORD ~queue:* +@all -@admin -flushall -flushdb -keys
   user ops   on >OPS_PASSWORD ~* +@all
   ```

   All of the service's keys start with `queue:`. **Before using this ACL in staging, run the integration suite once against a test-stack Redis with the same ACL user**, to prove no command the service needs is blocked.
7. **Recovery and monitoring:**
   - A CloudWatch alarm on `StatusCheckFailed_System` with the **EC2 recover action**. It keeps the private IP and the EBS volume, and Redis replays the AOF on start.
   - The CloudWatch agent for memory and disk, plus alarms: memory > 70% of `maxmemory`, disk > 70%, CPU > 70% sustained.
   - Patch monthly through SSM Patch Manager, **outside sale windows**. A restart loses nothing but takes the waiting room offline for the restart time.
8. **Smoke check** from inside the VPC (an ECS Exec shell in an app task, or SSM on the box): `redis-cli -h redis.queue.<env>.internal --user queue --pass … PING` gives `PONG`, `CONFIG GET maxmemory-policy` (as `ops`) gives `noeviction`.

### 10.6 RabbitMQ on the central broker

We don't run or configure the broker. We need:

1. **A vhost** per environment (or the shared one), and **a user `queue-service`** with permissions scoped to what we touch:

   | Permission | Regex | Why |
   | --- | --- | --- |
   | configure | `^(queue-service\..*\|queue\.events)$` | declare our queues, DLX and our own exchange (`own` mode) |
   | write | `^(queue-service\..*\|queue\.events\|amq\.default)$` | bind our queues, publish our events, DLQ replay via the default exchange |
   | read | `^(queue-service\..*\|event\.events\|reservation\.events\|checkout\.events)$` | consume our queues; bind them to the other services' exchanges |

   In `check` mode the broker team creates the queues, the DLX and the bindings exactly as `topology.py` describes: quorum queues, `x-delivery-limit: 4`, `x-dead-letter-exchange: queue-service.dlx`, `x-dead-letter-routing-key: <queue>.dlq`. Send them that file's definitions.
2. **The other teams' exchanges must exist** (`event.events`, `reservation.events`, `checkout.events`, topic, durable) before our consumer starts. In `own` mode it refuses to start without them, which surfaces a missing exchange early.
3. **First connection test:** run the `dlq list` ops task (10.8). It connects, and on success prints the three DLQ depths (0).
4. Agree on **maintenance windows**. The consumer reconnects with backoff, but confirm once in staging that it survives a broker node restart (the consumer task should reconnect, not crash-loop).

### 10.7 Build and push the image (CI)

1. In GitLab CI, after lint and tests: `docker build` → tag with the commit SHA → push to ECR. Build for the architecture Fargate will run (`linux/amd64`, or `linux/arm64` for Graviton/cheaper). **Never** use `latest` for deployments.
2. The image already holds no secrets (`.dockerignore`). Add the RDS CA bundle (10.4 step 4).

### 10.8 ECS: cluster, task definitions, services

1. **Cluster** `queue-service-<env>` (Fargate), with Container Insights on.
2. **Task definitions**, all from `queue-service:<sha>`, all with `awslogs`, and with secrets from 10.3 via the `secrets` block. Starting sizes; tune them from the load smoke:

   | Task def | Command | CPU / memory | Container health check | Secrets |
   | --- | --- | --- | --- | --- |
   | `queue-api` | *(image default: uvicorn on 8000)* | 0.5 vCPU / 1 GB | none (the load balancer checks it) | mysql, redis, rabbitmq, jwt |
   | `queue-admitter` | `python -m workers.admitter` | 0.25 vCPU / 0.5 GB | `python -m workers.healthcheck admitter` (interval 10 s, retries 3, start 10 s) | mysql, redis, rabbitmq |
   | `queue-consumer` | `python -m workers.consumer` | 0.25 vCPU / 0.5 GB | `python -m workers.healthcheck consumer` (interval 15 s, retries 3, start 20 s) | mysql, redis, rabbitmq |
   | `queue-migrate` | `alembic upgrade head` | 0.25 / 0.5 | — | mysql-**migrate** as `MYSQL_URL`, redis, rabbitmq |
   | `queue-ops` | overridden per run (`python -m workers.dlq list`, …) | 0.25 / 0.5 | — | mysql, redis, rabbitmq |

   Set `stopTimeout` to 30 s. The workers finish their current tick or message on SIGTERM, so this is plenty. Until code change B makes the JWT settings optional, the workers also need `JWT_KID` and the key secret.
3. **Services:**
   - **api:** desired 2 (one per AZ), autoscaling on CPU (~60%) and ALB request count. Rolling deploy with min 100% / max 200%. Deployment circuit breaker with rollback.
   - **admitter:** desired **1**, **`minimumHealthyPercent = 0`, `maximumPercent = 100`**. ECS's default (100/200) starts the new task before stopping the old one, giving **two admitters** for the length of every deploy. With 0/100 the old task stops first and admission pauses for a few seconds, which is acceptable. **No autoscaling, ever.** Circuit breaker on.
   - **consumer:** desired 1 (2 for redundancy; handlers are idempotent and quorum queues share the work). Rolling 100/200 is fine. Circuit breaker on.
   - Turn on **ECS Exec** (`enableExecuteCommand`) for ad-hoc `redis-cli`/ops access, if security allows.

### 10.9 Load balancer and routing

1. **Buyer traffic:** gateway (or public ALB) → target group `queue-api` on port 8000, forwarding **only `/api/v1/*`**. The gateway authenticates the buyer and sets `X-User-Sub`, and strips any client-supplied `X-User-Sub`.
2. **Internal traffic** (Reservation fetching JWKS, admin tooling): an **internal ALB** (or ECS Service Connect) → the same target group, for `/internal/v1/*` and `/.well-known/jwks.json`. It's reachable only from the VPC.
3. **Target group health check:** `GET /health/ready`, interval 10 s, timeout 3 s, healthy 2, unhealthy 3. Deregistration delay 30 s.
4. Give the Reservation team the JWKS URL for each environment (e.g. `https://queue.internal.<env>/.well-known/jwks.json`).

### 10.10 Deploy pipeline (every release)

1. Build and push `queue-service:<sha>` (10.7).
2. Register new revisions of all task definitions with the new image.
3. **Migrate:** `aws ecs run-task --task-definition queue-migrate:<rev> …`. Wait for it to stop and **check that the exit code is 0**; if not, stop the pipeline. Migrations must stay backward-compatible with the previous release, since old tasks keep running until the roll finishes.
4. Update the services in this order, waiting for each to stabilise (`aws ecs wait services-stable`):
   1. **consumer**
   2. **admitter**
   3. **api**
5. Post-deploy smoke: `GET /health/ready` through both load balancers, `GET /.well-known/jwks.json` returns the expected `kid`, and the admitter and consumer services show `HEALTHY`.
6. **Rollback:** update the services back to the previous task definition revisions. Don't downgrade the schema. If a migration was bad, fix forward with a new migration.

### 10.11 Observability

- **Logs:** JSON to CloudWatch. Create metric filters + alarms for:
  - `slow admitter tick`
  - `admission_integrity_mismatch`
  - `poison message dead-lettered`
  - `event_update_rejected` (informational)
  - ERROR-level count
- **ECS:** an alarm when the admitter's running count ≠ 1 for more than 1 minute, when consumer running < desired, and on api 5xx / target 5xx and p95 latency (ALB metrics).
- **Redis:** alarms from 10.5. **RDS:** our connection count vs budget (ask the RDS owners for access to the metrics). **Broker:** DLQ depth > 0 and consumer count on our three queues (broker team's monitoring, or a scheduled `dlq list`).
- Route alarms to the team's on-call channel.

### 10.12 Staging verification (before prod)

1. Bring staging up via 10.2–10.10.
2. Publish a test `EventPublished` to staging's `event.events` (as the Event team would, or with their help) with `release_at` ~20 min ahead and a short `sale_ends_at`.
3. Run the Phase 8 E2E scenario and `scripts/load_smoke.py` against the staging API (through the gateway, so `X-User-Sub` handling is exercised) at the expected peak buyer count. Check that:
   - the pool never exceeds capacity
   - admission continues during an **admitter deploy** (only a brief pause, no double admission)
   - p95 latency is acceptable
   - Redis memory and CPU stay within the alarm thresholds
4. **Failure drills:**
   - Restart the Redis service (readiness goes 503, then recovers; the AOF restores the queues).
   - Kill the admitter task (ECS replaces it, and the heartbeat alarm fires and clears).
   - Ask the broker team for a node restart (the consumer reconnects).
   - Fail an RDS connection, e.g. a wrong secret in a test task (clear error logs, not a hang).
5. Have the Reservation team validate a staging token against the staging JWKS.

### 10.13 Production go-live

1. Repeat 10.2–10.9 for prod with prod secrets, a prod key pair and prod sizing.
2. Deploy (10.10) **well before** the first event's `queue_opens_at`. Queue only learns about events from `EventPublished`, so **events published before the consumer was running are unknown to Queue**. Ask the Event team to republish (or send `EventUpdated` with a higher version) for any event published earlier.
3. Watch the first sale window live: admitter ticks, pool counts (`GET /internal/v1/events/{id}/queue`), DLQs and Redis memory.

### 10.14 Runbook updates (README, Operations)

- Add the AWS-specific parts: how to run `migrate`/`dlq` as ECS one-off tasks, how to open a `redis-cli` session, Redis restart/recovery and patching, the admitter deployment settings and why, and JWT key rotation with `JWT_PUBLIC_KEYS` (publish the new key, switch `JWT_KID` + secret, remove the old public key after 11 minutes).

**Done when** staging passes 10.12, prod is deployed with all alarms in place, and the runbook covers the AWS operations.

---

## Spec follow-ups (not code)

- Update the readiness line in the contracts doc to "MySQL and Redis" for the API.
- Remove the section 3 "check for existing reservation before selection-timeout release" paragraph (already agreed).
- Note the CLOSED join behaviour (200 with existing admission) in the POST `/queue/entries` section.
- The contracts doc says the token is "stored" at admission. CLAUDE.md signs on demand (deterministic RS256), which gives the same observable behaviour. Align the wording. It should also say that after a key rotation the same admission arrives as a different token string (already in the Endpoint reference's token contract).
- Share the "Admission token contract (for Reservation)" section of `docs/Endpoint reference.md` with the Reservation team.
- Still open: auth for `/internal/v1`, reporting rejected EventUpdated back to Event, and a global cap across event pools.
- ~~Events that never sell out stay live forever~~ → resolved by `sale_ends_at` (Phase 6b). Events published *without* it still never end; the Event team should send it for every event.
- Ask the Event team to add the optional `sale_ends_at` to EventPublished/EventUpdated (contract updated in the contracts doc).
- **KIV (2026-10-17): token reuse after an order is confirmed.** Admission tokens are self-contained JWTs that Queue can't revoke. OrderConfirmed ends the admission in Queue (the slot is freed, `/queue/me` shows NONE, the internal lookup gives 404), but the buyer's token still validates until `selection_expires_at` (≤5 min after admission) and could create another hold. **Preferred fix: option A.** Reservation refuses new holds for an `admission_id` that already has a confirmed order. That adds a check 9 to the token contract in `docs/Endpoint reference.md` and to `tests/fakes/reservation_service.py`; no Queue code changes. Rejected alternatives: B (a live call to `GET /internal/v1/admissions/{id}` per hold: a runtime dependency on Queue) and C (Reservation consuming Phase 9's `AdmissionReleased`: eventually consistent, more moving parts). On hold until the Reservation team is ready to discuss.
  - **Update (2026-10-20): covered by Reservation's existing check.** Per the developer, Reservation already checks whether a user has previously made a purchase or hold, so a leftover token can't be reused after OrderConfirmed. Queue's part is only to stop the buyer jumping the queue, and the release script already does that: it deletes the admission hash, the `user_adm` entry and the `active` pool member (and Python removes the `admission_index` entry). So `/queue/me` returns NONE with no token (tokens are signed on demand from the hash), a rejoin goes to the **back** of the waiting queue, and the freed slot goes straight to the next buyer. No Queue code changes.
  - **Still to confirm with the Reservation team: is their check per `admission_id` or per user + event?**
    - Per admission: rejoining and buying again works as today; nothing changes.
    - Per user + event ("already bought for this event"): a buyer who rejoins after an order waits through the queue, gets admitted and holds a pool slot for up to 11 minutes, only to be refused by Reservation. If that's the rule, consider refusing the rejoin in Queue. That changes the domain rule "after an admission ends, a buyer may join again at the back", so it's a product decision.
    - Either way, add the check (as Reservation actually implements it) to the token contract in `docs/Endpoint reference.md` and to `tests/fakes/reservation_service.py`.

## Suggested order and milestones

| Milestone | Phases | Demonstrable outcome |
| --- | --- | --- |
| M1 | 0–2 | Lua scripts proven correct under concurrency against real Redis |
| M2 | 3–5 | Buyer and internal API usable with manually seeded config |
| M3 | 6 | Full join → admit → expire loop running locally |
| M4 | 7 | Driven entirely by RabbitMQ messages |
| M5 | 8 | E2E, load smoke, containers, runbook |
| M6 | 9 | Audit messages (optional) |
| M7 | 10 | Running on ECS against shared RDS, central RabbitMQ and our Redis EC2, verified in staging, then prod |
