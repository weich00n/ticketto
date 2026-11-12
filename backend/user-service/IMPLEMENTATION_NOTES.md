# User Service: Implementation Notes

This file records what has been implemented, the design considerations behind it, and what is still left to do. The plan it was built from is [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). How to run the service is in [README.md](README.md).

**Status (2026-10-07):** all planned features are implemented. 53 unit tests and 24 integration tests pass (77 in total), and ruff and pylint are clean. A GitLab CI/CD pipeline lints, tests and builds every MR and deploys `main` to ECS (see Section 7). The AWS resources it deploys to haven't been created yet.

## 1. Decisions Used (from the plan's Q&A)

| Question | Decision | Effect on the code |
|---|---|---|
| Q1 Framework | FastAPI | |
| Q2 Source of `email` / `name` | Fetched from Cognito with `AdminGetUser` | `POST /users/me` ignores any request body |
| Q3 PATCH name sync | Also updated in Cognito with `AdminUpdateUserAttributes` | Cognito is updated first, then the DB |
| Q4 Extra scope | `GET /internal/v1/users/{user_id}`. CI/CD was added later as a separate request (Section 7) | |

## 2. What Was Built

| File | Contents |
|---|---|
| `app/main.py` | `create_app()`: logging, middleware, error handlers, routers |
| `app/config.py` | `Settings` (pydantic-settings, reads env vars and `.env`); builds the MySQL URL and connect args |
| `app/db.py` | `Base`, a lazily created engine, the `get_db` session dependency |
| `app/models.py` | `User` model matching the `users` table in `cognito-settings.md` §5 |
| `app/schemas.py` | `UserResponse`, `OrganiserUpgradeResponse`, `UserPatch`, error schemas, UTC timestamp formatting |
| `app/auth.py` | `get_current_user` (reads the `X-User-*` headers), `parse_groups()` |
| `app/cognito.py` | `CognitoClient` wrapper (`get_user_attributes`, `add_to_organiser_group`, `update_name`); converts boto errors into two exceptions |
| `app/context.py` | The request's correlation ID (`ContextVar`) and `get_correlation_id()`. Separate module so `correlation.py` and `errors.py` don't import each other |
| `app/errors.py` | `ApiError`, standard error responses, all exception handlers |
| `app/correlation.py` | ASGI middleware: correlation ID, access log, catch-all 500 |
| `app/logging_config.py` | JSON log formatter |
| `app/routers/users.py` | The four `/api/v1/users/me` routes |
| `app/routers/internal.py` | `GET /internal/v1/users/{user_id}` |
| `app/routers/health.py` | `/health`, `/health/ready` |
| `alembic/versions/0001_create_users.py` | Creates `users` (MySQL-specific defaults, InnoDB, utf8mb4) |
| `Dockerfile` | Multi-stage on `python:3.12-slim`: `base` (runtime deps), `test` (adds dev deps; used by the `tests` service), `runtime` (production: non-root user uid 10001, RDS CA bundle, `HEALTHCHECK`). `runtime` is last, so a plain `docker build` produces the production image |
| `docker-compose.yml` | `mysql:8.4` (host port 3307, named volume, health check), `cognito-local`, `user-service` (with a 3-second health check), and `tests` (in the `test` profile; runs the integration suite on the Compose network) |
| `scripts/seed-cognito-local.sh` | Creates the local pool, client, `organiser` group and test users, and writes the pool ID into `.env`; safe to re-run |
| `.env.example`, `.gitignore`, `.dockerignore` | `.env` and `.cognito/` are git-ignored |
| `tests/unit/` | 53 unit tests (see Section 6) |
| `tests/integration/` | 24 integration tests against the running Compose stack (see Section 6) |
| `pytest.ini` | Registers the `integration` marker; a plain `pytest` runs only the unit tests |
| `pyproject.toml` | Tool settings only: ruff, pylint, coverage |
| `.gitlab-ci.yml` | The CI/CD pipeline (Section 7) |
| `scripts/ci/run-integration-tests.sh` | CI integration job: waits for MySQL and cognito-local, creates a pool, migrates, starts the service, runs `tests/integration` |
| `scripts/ci/ecs-migrate.sh`, `scripts/ci/ecs-deploy.sh` | Deploy steps: register the task definition and run the migration task; update the ECS service and detect rollbacks |
| `deploy/task-definition.json` | ECS Fargate task definition template with `__PLACEHOLDER__` values |
| `deploy/README.md` | One-time AWS and GitLab setup for deployment |
| `README.md` | Replaces the GitLab template |

## 3. Endpoints as Implemented

Every response, success or error, includes `correlation_id` in the body and an `X-Correlation-Id` response header. The profile object is:

```json
{
  "user_id": "64b422d5-d377-4b52-9724-dbf4b8c77d96",
  "email": "buyer@test.com",
  "name": "Test Buyer",
  "role": "buyer",
  "created_at": "2026-10-06T11:36:20Z",
  "updated_at": "2026-10-06T11:36:20Z",
  "correlation_id": "9e10f6a5-6657-4d38-b8ae-d7b00f01ad6d"
}
```

`cognito_sub` is never returned.

### `GET /api/v1/users/me`
Returns 200 with the profile. Returns 404 `USER_NOT_FOUND` if there is no profile, and 401 `UNAUTHENTICATED` if the headers are missing.

### `POST /api/v1/users/me`
1. If a profile exists for `X-User-Sub`, return **200** with it. Cognito is not called on this path, so repeat logins still work during a Cognito outage.
2. Otherwise call `AdminGetUser(X-User-Username)` and read `email`, `name` and `sub`.
3. Insert the row with `role = buyer` and a new UUID, then return **201**.

| Status | When |
|---|---|
| 201 | Profile created |
| 200 | Profile already existed |
| 401 `UNAUTHENTICATED` | Headers missing; Cognito user not found; or Cognito `sub` ≠ `X-User-Sub` |
| 503 `COGNITO_UNAVAILABLE` | Cognito failed or timed out; nothing is written |

### `PATCH /api/v1/users/me`
- The body must be exactly `{"name": "<1–255 chars after trimming>"}`.
- Any other field returns 400 `VALIDATION_ERROR`, e.g. `"role: field is not allowed"`. So does `{}`, a non-string name, or malformed JSON.
- If the name changed: Cognito `AdminUpdateUserAttributes` first, then the DB. If Cognito fails → 503 and the DB is unchanged.
- If the name is the same as the current one, nothing is written and Cognito is not called.

### `POST /api/v1/users/me/organiser`
This follows `cognito-settings.md` §7:
1. Look up the profile (404 if none).
2. Call `AdminAddUserToGroup` (503 on failure; the DB is unchanged).
3. Set `role = organiser` if it isn't already.

The response is the profile plus `token_refresh_required`. That is `true` unless `organiser` is already in the caller's `X-User-Groups`. The group check uses exact set membership via `parse_groups()`.

### `GET /internal/v1/users/{user_id}`
Returns 200 with the profile. Returns 404 `USER_NOT_FOUND` if the ID doesn't exist, and 400 `VALIDATION_ERROR` if it isn't a UUID. Upper-case UUIDs are accepted. It does not use the `X-User-*` headers.

### `GET /health` and `GET /health/ready`

| Route | Response |
|---|---|
| `/health` | `{"status":"ok"}`. Never touches the DB |
| `/health/ready` | `{"status":"ok","checks":{"database":"up"}}`; or 503 with `"database":"down"` |

## 4. Design Considerations

### Identity and security
- **Identity:** the caller is identified only by `X-User-Sub` (with `X-User-Username` for Cognito admin calls). User IDs in the body or query string are never trusted. This is only safe if the service is reachable **only** through API Gateway (see Section 8).
- **`username` vs `sub`:** with email sign-in, Cognito's `username` is a UUID equal to `sub`, not the email address. This was confirmed against cognito-local. The service passes `X-User-Username` to the admin APIs, which works either way.
- **Defensive `sub` check:** on profile creation, the `sub` that `AdminGetUser` returns must match `X-User-Sub`. If it doesn't, the service returns 401 and writes nothing. This catches misconfigured header mapping before it links a profile to the wrong account.
- **`UserNotFoundException` → 401, not 503:** the token was valid but the Cognito user is gone, e.g. deleted by an admin. Retrying won't help, so the service doesn't return "temporary failure".
- **Name fallback:** if a Cognito user has no `name` attribute (possible for admin-created users), the part of the email before `@` is used, so login doesn't fail permanently. A user with no `email` causes a 500, because that should never happen.
- **Internal endpoint:** the internal endpoint has no authentication in code. It relies on having no API Gateway route plus security groups. If stronger protection is needed later, a shared header or IAM auth can be added without changing callers' URLs.

### Email and duplicate accounts
- **Duplicate sign-ups are stopped by Cognito**, not by this service. The pool uses email as the sign-in identifier, so signing up with an existing email fails with `UsernameExistsException` (handled by the Next.js routes) and no second account reaches `POST /users/me`.
- **The service never looks users up by email.** Profiles are matched by `cognito_sub` only, and `email` has **no unique constraint** in MySQL.
- **Known edge case:** if a Cognito account is deleted and the person signs up again with the same email, they get a new `sub`. The service then creates a **second row with the same email**, and the old row stays behind as an orphan because there is no delete endpoint. Lookups still find the right profile, but anything tied to the old `user_id` (tickets, orders) stays with the old row.
- **Why no unique index on `email`:** it would make that same re-sign-up fail with an `IntegrityError`, leaving the person unable to create a profile. It would only be safe together with a clean-up or reassignment rule for the old row.
- **Email is a snapshot.** It is copied from Cognito once, at first login. There is no email-change flow (`PATCH` only changes `name`), so a later change in Cognito is not reflected in MySQL.

### Consistency between Cognito and MySQL
- **Ordering:** for both writes that touch Cognito (organiser, name), Cognito is called first. Both steps are idempotent, so if the DB write fails the client can safely retry and the two converge. The DB never claims something Cognito doesn't reflect.
- **First-login race:** two concurrent `POST /users/me` calls can both miss the lookup. The `UNIQUE(cognito_sub)` constraint makes the second insert fail. The service then rolls back, re-reads the row and returns 200 with the winner's profile. Tests cover this by inserting a row between the lookup and the insert.

### Database
- **Time zone:** every MySQL connection runs `SET time_zone = '+00:00'`, so `TIMESTAMP` values are read and written as UTC regardless of the RDS parameter group. Responses are formatted as `YYYY-MM-DDTHH:MM:SSZ`.
- **Schema match:** the migration matches `cognito-settings.md` §5 exactly (`CHAR(36)` IDs, `ENUM('buyer','organiser')`, `TIMESTAMP` with `ON UPDATE CURRENT_TIMESTAMP`). `TIMESTAMP` only goes up to the year 2038; that's acceptable for this project, but noted.
- **Connections:** `pool_pre_ping` and `pool_recycle=3600` handle connections that RDS or a NAT has closed. TLS is enabled only when `DB_SSL_CA` is set. The image already contains the RDS global CA bundle at `/etc/ssl/rds/global-bundle.pem`.
- **Credentials:** the DB URL is built from separate `DB_*` variables with `sqlalchemy.URL.create`, so passwords with special characters work. `alembic.ini` has no URL; `alembic/env.py` uses the app's engine.
- **`cryptography` dependency:** included because PyMySQL needs it for MySQL 8's default `caching_sha2_password` authentication.

### Migrations
- **Locally:** Compose runs `alembic upgrade head && uvicorn ...`.
- **In AWS:** the image's default command does **not** migrate, so several ECS tasks starting together can't race on DDL. Run `alembic upgrade head` as a one-off task per deployment.

### Cognito client
- boto3 with a 5-second connect and read timeout (`COGNITO_TIMEOUT_SECONDS`) and 2 attempts in "standard" retry mode, so a slow Cognito turns into a 503 quickly instead of holding a worker.
- The client is created once and reused.
- Tests replace it via FastAPI dependency overrides.

### Errors
- Handlers cover:
  - our own `ApiError`
  - validation errors (422 → **400** `VALIDATION_ERROR`, with a readable message)
  - Starlette HTTP errors (unknown route → 404 `NOT_FOUND`, wrong method → 405 `METHOD_NOT_ALLOWED`)
  - the Cognito exceptions
- **Unexpected exceptions** are caught in the correlation middleware rather than a FastAPI handler. That way a 500 still gets the correlation ID header and body. Exception text is logged but never returned to the client.

### Correlation ID and logging
- **Correlation ID:**
  - Taken from `X-Correlation-Id` only if it matches `[A-Za-z0-9._-]{1,128}`. Otherwise a new UUID is generated, which prevents log injection through the header.
  - Stored in a `ContextVar`. Confirmed that it reaches log lines written inside sync endpoints, which run in a thread pool.
- **Logs:** one JSON object per line with `timestamp`, `level`, `logger`, `message`, `correlation_id`, plus any `extra=` fields.
  - One access-log line per request: method, path, status, `duration_ms`. Health-check requests are logged at DEBUG to keep ALB pings out of the logs.
  - Uvicorn's own access log is disabled.
  - Tokens, passwords and request bodies are never logged.

### Code quality
- **ruff** (`pyproject.toml`): rules E, F, W, I, B, UP, line length 100. `B008` is off because FastAPI's `Depends()` / `Header()` defaults are the framework's intended style. `alembic` is declared third-party for import sorting, since the local `alembic/` folder would otherwise make ruff treat it as first-party.
- **pylint** (`pyproject.toml`): docstring checks, `redefined-outer-name` (every pytest fixture triggers it), `too-few-public-methods` and `wrong-import-position` (the unit `conftest.py` sets an env var before importing the app) are off. `recursive = true` lets it lint `alembic/`, which has no `__init__.py` on purpose: adding one would shadow the installed Alembic library.
- **Remaining findings were fixed, not hidden**, except where the tool is wrong:
  - **Circular import fixed:** `correlation.py` and `errors.py` imported each other; the correlation ID moved to `app/context.py`.
  - **SQLAlchemy false positives avoided:** `func.now()` became the equivalent `functions.now()`, and the `sessionmaker[Session]` annotation is a string, so pylint doesn't misread them.
  - **Integration fixtures narrowed:** they catch specific exceptions (`ClientError`, `BotoCoreError`, `SQLAlchemyError`) instead of `Exception`.
  - **Inline disables, three places, each with a reason:** the middleware's last-resort `except Exception`, Alembic's runtime-generated `op` / `context`, and Alembic's required lower-case `revision` names.
- Result: ruff clean, pylint 10.00/10, verified on Python 3.12 (the CI and image version) in a clean container.

### Docker
- Runs as a non-root user.
- `.dockerignore` keeps `.env`, tests and git data out of the image.
- The `tests` container is created fresh by `docker compose run --rm tests` and deleted when pytest exits (pass or fail). The built image stays cached; the other three containers keep running.
- MySQL's Compose health check uses TCP (`-h 127.0.0.1`). The socket would report healthy during MySQL's temporary init server, before the database and user exist.

## 5. Differences from the Plan

| Plan | Implemented | Reason |
|---|---|---|
| Organiser response fields as in `cognito-settings.md` §7 | Same fields **plus** `created_at` / `updated_at` | One profile shape for all user endpoints; extra fields don't break clients |
| PATCH body `{"name": ...}` | `name` is **required**; `{}` returns 400 | It's the only editable field, so an empty PATCH is almost certainly a client bug |
| `mysql:8` | `mysql:8.4` on host port **3307** | 8.4 is the current LTS; 3307 avoids clashing with a locally installed MySQL |
| Not in plan | `sub` mismatch check, name fallback, `UserNotFoundException` → 401, 405 error code | See Section 4 |

## 6. Testing

### Running the suites
`pytest.ini` has `addopts = -m "not integration"`, so a plain `pytest` collects all 77 tests but **deselects** the 24 integration ones and runs the 53 unit tests (`53 passed, 24 deselected`). A `-m` on the command line replaces that default:

| Command | Runs |
|---|---|
| `pytest` / `pytest tests/unit` | 53 unit tests |
| `pytest -m integration` | 24 integration tests (`53 deselected`) |
| `pytest -m ""` | **All 77.** The empty expression means "no filter". Needs the stack running |

In Docker, run the same commands as `docker compose run --rm tests pytest ...`. With no arguments, `docker compose run --rm tests` runs the integration suite.

Test folders: unit tests are in `tests/unit/` (with their own `conftest.py` and fakes) and integration tests are in `tests/integration/`.

### Unit tests
53 tests using in-memory SQLite and `FakeCognito`; no stack needed. Run them with `docker compose run --rm --no-deps tests pytest`, or `python -m pytest` with the venv active. A plain `pytest` (no `-m`) runs only these, because `pytest.ini` deselects the integration tests. The files are in `tests/unit/`, and the integration tests are in `tests/integration/`.

| File | Covers |
|---|---|
| `test_users_me.py` | All four `/users/me` routes, including: <br>• idempotency and the insert race <br>• body ignored on POST <br>• unknown/invalid PATCH fields <br>• Cognito failures leaving the DB unchanged <br>• `token_refresh_required` cases <br>• `sub` mismatch <br>• name fallback |
| `test_internal.py` | Found, not found, invalid UUID, upper-case UUID |
| `test_health.py` | Liveness, readiness up/down, liveness not touching the DB |
| `test_cross_cutting.py` | `parse_groups()` variants and exact matching; correlation ID echo/generate/sanitise; 404/405/400/500 error format; JSON log fields |

### Integration tests
24 tests against the **running** Compose stack. They:
- call the service over HTTP,
- create and inspect users in cognito-local with boto3,
- query MySQL directly to check what was actually saved.

| Run from | Command | Addresses used |
|---|---|---|
| Docker (recommended) | `docker compose run --rm tests` | `user-service:8000`, `cognito-local:9229`, `mysql:3306` |
| Your machine (venv active) | `python -m pytest -m integration` | `localhost:8000`, `localhost:9229`, `127.0.0.1:3307` |

| File | Covers |
|---|---|
| `test_schema.py` | Alembic is at revision `0001`; column types, keys, defaults and `ON UPDATE CURRENT_TIMESTAMP` match the spec; the `role` ENUM rejects other values |
| `test_health.py` | Liveness; readiness against real MySQL |
| `test_users_me.py` | First login reads email/name from Cognito and writes the row; idempotency; request body can't override Cognito data; **10 concurrent first logins create exactly one row**; Cognito-deleted user gets 401; PATCH updates MySQL **and** Cognito and bumps `updated_at`; protected fields rejected with the DB unchanged; users isolated from each other |
| `test_organiser.py` | Full flow with **real cognito-local tokens**: sign in → create profile → upgrade (`token_refresh_required: true`) → new token contains `cognito:groups: ["organiser"]` → upgrade again with that group header (`false`); 404 path leaves Cognito unchanged |
| `test_internal_and_errors.py` | Internal lookup; correlation ID round trip; error format for unknown routes |

**Design choices:**
- **Isolation:** each test creates its own Cognito user (`it-<random>@test.com`) and the fixture deletes it and its MySQL row afterwards. Verified: no `it-` users or rows remain after a run. Tests don't depend on the seed users or on each other.
- **Configuration:** values are read from `.env` rather than `os.environ`, because the unit-test `conftest.py` sets a fake `COGNITO_USER_POOL_ID` in the environment. Any value can be overridden with an `IT_` prefix, e.g. `IT_SERVICE_URL`.
- **Stack down:** if the service, MySQL or the Cognito pool is unreachable, the session fixtures **fail** with instructions rather than skipping, so a stopped stack can't look like a pass.
- **Concurrency test:** it fires 10 simultaneous first logins and checks the outcome: one 201, nine 200s, one row. Whether the requests actually overlap in the race window depends on timing. The unit test forces that path deterministically.
- **Running in Docker:** `docker compose run --rm tests`.
  - The `tests` service builds the `test` Dockerfile stage and mounts the source at `/src`, so there's no rebuild after code changes.
  - It reaches the other containers by service name, using `IT_SERVICE_URL=http://user-service:8000`, `IT_COGNITO_ENDPOINT=http://cognito-local:9229`, `IT_DB_HOST=mysql` and `IT_DB_PORT=3306`. Inside a container, `localhost` would mean the test container itself.
  - It `depends_on` `user-service` being **healthy**, so one command starts the whole stack and waits for it. Verified from a fully stopped stack.
  - It's in the `test` profile, so `docker compose up` doesn't start it. This is also the shape a GitLab CI job would use.
- **Isolation, and its limits:**
  - Tests are independent of each other: unique users, nothing relies on another test's data or on the seed users.
  - They do depend on the environment: a seeded Cognito pool whose ID is in `.env`. Each developer's pool has a different random ID, which doesn't matter because each machine tests its own local stack.
  - They share the pool and MySQL database with whatever you create by hand. Unique emails avoid collisions, but it isn't a sandbox.
  - Fixture clean-up runs even when a test fails, but not if the run is killed (e.g. Ctrl+C), which can leave stray `it-` users and rows behind.
  - A stronger option, not implemented: a separate Compose project for tests (own MySQL, cognito-local and service), created and destroyed per run with `down -v`. This is also the natural shape for a CI job.
- **Not covered here:** Cognito or MySQL outages. Triggering them would mean stopping shared containers mid-run, so they stay in the unit tests (fake Cognito, broken session).

### Manual end-to-end check
Run against Docker Compose with MySQL 8.4 and cognito-local, using a real user created by the seed script:

1. The migration created the table exactly as specified (checked with `SHOW CREATE TABLE`).
2. Requests and results:
   - GET returned 404.
   - POST returned 201, then 200 on a repeat call.
   - PATCH returned 200, and the name changed in **both** MySQL and Cognito.
   - PATCH with `role` returned 400.
   - The organiser upgrade returned 200 with `token_refresh_required: true`. Repeated with `X-User-Groups: [organiser]`, it returned `false`.
   - The internal lookup returned 200, readiness returned 200, and a request without headers returned 401.
3. A new token issued after the upgrade contained `"cognito:groups":["organiser"]`.

**What `.cognito/` is:** cognito-local's data folder, mounted into its container (`./.cognito:/app/.cognito`). It holds `config.json` and `db/<pool-id>.json` with the pool, app clients, groups and users (including plain-text test passwords). It is why seeded users survive `docker compose down`. It is **git-ignored and should not be committed**: it is rewritten on every sign-in, holds secrets and is tied to one machine's pool ID. Teammates recreate it with `.env.example` plus `scripts/seed-cognito-local.sh`, both of which are committed. It is separate from MySQL, whose data lives in the `mysql-data` Docker volume. The project folder is synced by OneDrive, which also syncs `.cognito/` and `.env`; this may be why the folder was reset once.

**Stale pool ID:** cognito-local creates pools with random IDs and keeps them in `.cognito/`. If that folder is lost, `.env` points at a pool that doesn't exist and the integration tests fail with "Cognito pool ... not found". Re-run the seed script (it now updates `.env` automatically) and then `docker compose up -d user-service`; Compose recreates the container because its environment changed.

**Quirk:** cognito-local's `admin-list-groups-for-user` returned an empty list even though the group was applied. To check group membership locally, decode a new token instead.

## 7. CI/CD

### Pipeline (`.gitlab-ci.yml`)
Runs for merge requests and for `main` (not twice for a branch with an open MR). Newer pipelines cancel older ones for the same ref, except deploy jobs.

| Stage | Job | What it does | Runs on |
|---|---|---|---|
| lint | `ruff` | `ruff check`, `ruff format --check`; also a Code Quality report for the MR widget | MR, main |
| lint | `pylint` | `pylint app tests alembic`; any message fails | MR, main |
| test | `unit-tests` | `pytest tests/unit` with coverage; JUnit and Cobertura reports, so results and coverage show in the MR | MR, main |
| test | `integration-tests` | MySQL 8.4 and cognito-local 5.3.0 as GitLab services; `scripts/ci/run-integration-tests.sh` | MR, main |
| test | `sast`, `secret_detection` | GitLab templates (Semgrep for Python; leaked-secret scan) | MR, main |
| build | `build-image` | `docker build --target runtime`, no push | MR |
| build | `build-and-push` | Build with the previous image as layer cache, push `:<sha>` and `:latest` to ECR | main |
| deploy | `migrate` | `scripts/ci/ecs-migrate.sh`: render and register the task definition, run `alembic upgrade head` as a one-off Fargate task, fail unless it exits 0 | main |
| deploy | `deploy` | `scripts/ci/ecs-deploy.sh`: update the ECS service, wait for it to be stable, fail if ECS rolled back | main |

### Design considerations
- **Integration tests use GitLab services, not Docker-in-Docker.**
  - Faster, and it avoids bind-mount problems with dind. The job creates its own Cognito pool and passes its ID through the existing `IT_*` overrides, so `.env` and `.cognito/` aren't needed in CI.
  - Trade-off: it tests the source, not the built image. The local `docker compose run --rm tests` tests the image; in AWS, the ECS and ALB health checks cover the runtime image.
- **AWS login with OIDC.** Jobs get a short-lived GitLab ID token, and the AWS CLI assumes `$AWS_ROLE_ARN` through `AWS_WEB_IDENTITY_TOKEN_FILE`. The IAM role trusts only `project_path:cs302-2026/g2-team5/backend/user-service:ref_type:branch:ref:main`, so MR pipelines (including from forks) can't reach AWS, and there are no long-lived keys to leak.
- **Deploy variables** are protected and scoped to the `production` environment, so only `build-and-push`, `migrate` and `deploy` receive them. `build-and-push` and `migrate` use `environment: action: prepare` to get the variables without being recorded as deployments.
- **Variable names don't reuse the app's** (`RDS_ENDPOINT`, `APP_COGNITO_USER_POOL_ID` instead of `DB_HOST`, `COGNITO_USER_POOL_ID`), because GitLab settings variables override `.gitlab-ci.yml` variables and would otherwise leak into the integration job.
- **The task definition is in the repo** (`deploy/task-definition.json`), so container settings are versioned and reviewed like code. The pipeline fills in `__PLACEHOLDER__` values with `sed` and fails if any placeholder is left unfilled.
- **Migrations run before the new version is rolled out**, as a one-off task using the new image. A failed migration stops the pipeline before any running task is replaced.
- **Rollback detection.** With the ECS circuit breaker, a failed rollout also ends "stable", but on the old revision. `ecs-deploy.sh` checks which revision is actually primary, so a rollback fails the pipeline instead of looking like success.
- **No overlapping deploys:** `resource_group: production`, and deploy jobs are not interruptible.
- **Pinned images:** `python:3.12-slim` (matches the Dockerfile), `mysql:8.4`, `jagregory/cognito-local:5.3.0` (also pinned in `docker-compose.yml`), `docker:29`, `amazon/aws-cli:2.37.9`.
- **Runtime platform** is set to `X86_64` in the task definition, matching the gitlab.com runners that build the image.

### What was verified, and what wasn't
- **Lint and unit jobs:** ran the exact job commands in a clean `python:3.12-slim` container. Ruff clean, pylint 10.00/10, 53 passed at 92% coverage; the report files were produced.
- **Integration job:** ran `run-integration-tests.sh` in a clean `python:3.12-slim` container with MySQL and cognito-local reachable by service name, as in CI. 24 passed. This also caught a bug: boto3 reads `AWS_DEFAULT_REGION`, not `AWS_REGION`, so the script now passes the region explicitly.
- **Build job:** `docker build --target runtime` succeeds.
- **Deploy scripts:** run against a fake `aws` command, covering success, failed migration, task not started, rollback detected and missing variable. All behave as intended, and the rendered task definition is valid JSON. ShellCheck is clean on all CI scripts.
- **Not verified:**
  - The pipeline has not run on GitLab yet: nothing is committed or pushed.
  - The GitLab-specific parts can only be confirmed by a real run: the SAST and secret-detection templates, OIDC, the `!reference` / `environment: action: prepare` syntax.
  - Nothing has run against real AWS, because the resources don't exist yet.
  - Validate the file in GitLab's pipeline editor (CI Lint) before the first push.

## 8. Follow-ups for the Team

**AWS setup** (step-by-step guide in `deploy/README.md`)
- [ ] Network: NAT gateway or VPC endpoints for the private subnets; security groups for the service and RDS.
- [ ] RDS database `db_user`, DB user and Secrets Manager secret (`username` / `password` keys).
- [ ] ECR repository `user-service`, CloudWatch log group `/ecs/user-service`.
- [ ] IAM: task execution role, task role, GitLab OIDC provider and deploy role.
- [ ] GitLab CI/CD variables (protected, `production` scope).
- [ ] First deployment: let `migrate` register the task definition, create the ECS service (circuit breaker + rollback on), then re-run `deploy`.
- [ ] Grant the User Service task role `cognito-idp:AdminGetUser`, `cognito-idp:AdminAddUserToGroup` and `cognito-idp:AdminUpdateUserAttributes` on the `us-east-1_2g8EYCEa1` pool. `cognito-settings.md` §7 and §9 list only `AdminAddUserToGroup`; update them.
- [ ] Create the `organiser` group in the real pool (`cognito-settings.md` §10).
- [ ] Security groups: the service must only accept traffic from the VPC Link and other services, because it trusts `X-User-*` headers. `/internal/v1/*` must have no API Gateway route.
- [ ] Verify the real `X-User-Groups` format coming from API Gateway, and add it to the `parse_groups()` tests if it's new.

**Contract and frontend**
- [ ] Add `POST /users/me/organiser`, `PATCH /users/me` and `GET /internal/v1/users/{user_id}` to the Technical Contract.
- [ ] Frontend: refresh tokens after `PATCH /users/me` (so the ID token's `name` updates) and when `token_refresh_required` is `true`.

**Decisions still open**
- [ ] What should happen when a profile already exists with the same email under a different `sub` (see "Email and duplicate accounts")? Options: leave as is, reject with 409, or reassign the old row to the new `sub`.
- [ ] Should a separate, disposable test stack be added for stronger isolation and CI?

**CI/CD**
- [ ] Validate `.gitlab-ci.yml` in GitLab's pipeline editor, then push a branch and open an MR to see the first real pipeline.
- [ ] Optional additions offered but not chosen: Trivy image scanning, Bandit, mypy, pip-audit.
- [ ] Once this pipeline is proven, move the shared parts into a group-level CI template so the other services `include:` it instead of copying.

**Repo setup**
- [ ] The `user-service` git remote was switched from HTTPS to SSH (`git@gitlab.com:cs302-2026/g2-team5/backend/user-service.git`) because the HTTPS URL made git ask for a password GitLab no longer accepts. Nothing has been committed or pushed yet.

**Not done (out of scope)**
- [ ] The local API gateway from `cognito-settings.md` §8.5.
