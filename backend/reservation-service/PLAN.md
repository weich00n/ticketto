# Reservation Service — Build Plan

> **Status (2026-11-06): every phase is built and live.** The service runs on ECS Fargate behind
> the shared gateway, deployed by the GitLab pipeline on every push to `main`. The phases below
> are kept as the record of how it was built and why; where a later change superseded a phase,
> the phase says so. "After the plan" at the end lists what was added since. How to run, test and
> deploy it is in `README.md` and `deploy/README.md`.

## Context

Reservation Service for the CS302 event-ticketing project (team g2-team5). It owns live
ticket inventory, temporary holds, the five-ticket purchase limit, and reservation
confirmation.

**Authoritative source: the Technical Contract tab.** Section 1 of that document states
changes affecting another service must be agreed there before implementation, so where the
API Documentation tab disagrees, the Technical Contract wins. The API Documentation tab is
being rewritten to match; until that lands, build against the contract.

- Java 21, Spring Boot 4.1.1, Maven
- MySQL — the durable record: inventory totals, sold counts, hold rows, event sale status;
  schema managed by Flyway (`src/main/resources/db/migration`)
- Redis — the live state: availability counter, hold liveness keys, expiry timer
- RabbitMQ — the shared broker, whose topology rabbitmq-service owns
- Local Redis + MySQL (and RabbitMQ for the tests) via `docker-compose.yml`
- GitLab: `gitlab.com/cs302-2026/g2-team5/backend/reservation-service`

### Design decision — supersedes System Design pp. 6 & 8 (doc being updated)

Tickets are sold **by category and quantity**, never by seat. A hold is "2 tickets in CAT1".

**Redis decides availability. MySQL remembers what happened.**

- Redis keeps `avail:{event}:<cat>` — tickets free *right now*, held and sold already
  subtracted out. One atomic Lua script performs the entire check-and-hold, so concurrent
  buyers cannot oversell.
- MySQL keeps one row per hold. This is what makes confirmation possible: Checkout calls
  minutes later with only a `hold_id`, and may retry. Once confirmed the Redis key is gone,
  so only the MySQL row distinguishes *already confirmed* (success) from *never existed*
  (Checkout refunds).
- A hold is confirmable **only while its Redis key is still alive**.
- Losing Redis is recoverable: every counter rebuilds from the MySQL rows. This is what
  satisfies the contract's acceptance check "Redis failure does not erase authoritative
  hold records."

---

## Rules (follow in every phase)

1. **Layers have one job each:**
   - `controller/` — HTTP in, DTO out. No business logic.
   - `service/` — all business logic.
   - `repository/` — data access only (JPA for MySQL, `StringRedisTemplate` + Lua for Redis).
   - `model/` — JPA entities and enums.
   - `dto/` — request/response records. **Controllers never return entities.**
2. **Redis key layout.** Every key carries a `{eventId}` hash tag so one event's keys stay
   in one slot:
   - `avail:{e}:<categoryId>` — integer, tickets available now
   - `hold:{e}:<holdId>` — hash + TTL; **its existence means the hold is alive**
   - `zexp:{e}:<categoryId>` — sorted set, score = expiry epoch, member = holdId
   - `userqty:{e}:<userId>` — integer, held + bought, for the five-ticket limit
   - `sale:{e}` — `ON_SALE` | `CANCELLED`
3. **Availability is never computed — only read, or changed atomically.**
   - *Changing* it happens only inside a Lua script (`DECRBY` / `INCRBY`). Never
     read-then-write from Java; that is the exact race this design exists to prevent.
   - *Reading* it means `GET avail:{e}:<cat>` and nothing else. Do **not** derive it as
     `total − held − sold`. The counter is the answer.
   - `held` is never stored. It is derived **only** for the advisory `capacity` payload
     (Phase 6), clamped at zero and documented as approximate.
   - The sole exception is seeding a counter that is missing (Phase 7).
4. Anything that differs between laptop and server uses `${ENV_VAR:default}` in
   `application.properties`. Never write a real password in a committed file.
5. After each phase: run `./mvnw test`, make sure it passes, then make one git commit with a
   clear message. Do not push unless I ask.
6. Work one phase at a time. Stop at the end of each phase and summarise what changed.
7. Keep explanations simple; I'm a student learning this.

## Contract conventions (Technical Contract §2, §9, §10)

| Item | Rule |
|---|---|
| Identifiers | **UUID strings** — `user_id`, `event_id`, `category_id`, `admission_id`, `hold_id`, `order_id` |
| Public prefix | `/api/v1` |
| Internal prefix | `/internal/v1` — not buyer-accessible |
| Time | UTC ISO-8601, e.g. `2026-11-20T12:00:00Z` |
| Money | integer, smallest unit (`28800` = SGD 288.00), currency `SGD` |
| Response body | **flat JSON — no `{"data": ...}` wrapper** |
| Hold status | `ACTIVE` / `CONFIRMED` / `EXPIRED` / `CANCELLED` (never `HELD`/`SOLD`) |
| Correlation | every request and message carries `correlation_id` |
| Idempotency | `POST /reservations` accepts `Idempotency-Key` |
| Authentication | **not this service's job** — see below |

### Authentication — settled by the Cognito setup notes (§6)

Services **never verify access tokens**. API Gateway's JWT authorizer verifies the Cognito
token and passes the verified claims down as headers, overwriting anything the client sent:

| Header | Claim |
|---|---|
| `X-User-Sub` | `sub` — the buyer's permanent Cognito id, and **the** authenticated identity |
| `X-User-Username` | `username` |
| `X-User-Groups` | `cognito:groups`, as a string such as `[organiser]` |

So this service reads the buyer from `X-User-Sub`, answers 401 `UNAUTHENTICATED` when it is
absent, and never trusts a `user_id` in a body or query string. This only holds while the
service is unreachable except through the gateway — the security groups must enforce that,
or anything reaching the service directly could impersonate a buyer.

### Admission token claims Queue must send

Queue signs the token; we verify it. Each claim below is load-bearing:

| Claim | Why we cannot do without it |
|---|---|
| `admission_id` | the hold is keyed to it; one hold per admission |
| `user_id` | the buyer, stored on the hold and counted by the five-ticket limit. The team's `user_id` **is** the Cognito `sub`, so it must equal the caller's `X-User-Sub`, or a shared or stolen token could hold tickets |
| `event_id` | otherwise an admission for one event would hold tickets for another |
| `selection_expires_at` | §6: an 11-minute token must not permit a hold after the 5-minute selection deadline. We cannot ask Queue at hold time — §6 says Queue must not depend on that call being available either |
| `admission_expires_at` | otherwise a token never expires and could hold tickets days later |

This is the §6 admission object exactly, which already says the token carries "the
corresponding identity and deadline claims". An earlier draft asked Queue for an extra
`user_sub` claim and skipped the buyer check without it; since Queue signs `user_id` from
`X-User-Sub`, the check now compares `user_id` directly and always runs.

### What Queue does not need to tell us

A hold row stays in MySQL for good; buying moves it from `ACTIVE` to `CONFIRMED` rather than
deleting it. So this service already knows, without Queue telling it:

- **An admission that was already used.** Before touching Redis, a hold attempt looks up its
  `admission_id` and refuses with 409 `ADMISSION_ALREADY_HAS_HOLD`, whatever the earlier
  hold's status: `ACTIVE`, `CONFIRMED`, `EXPIRED` or `CANCELLED`. The unique constraint on
  `admission_id` decides two attempts that arrive together. Queue never has to send "this
  admission is spent" or "this buyer already bought".
- **A buyer coming back with a fresh admission.** The admission check does not fire on a new
  `admission_id`; the five-ticket limit does. It counts tickets held **plus bought** per
  `user_id` per event, so a buyer who bought 5 gets 409 `PURCHASE_LIMIT_EXCEEDED`, and one who
  bought 2 may still hold 3. The count is rebuilt from MySQL if Redis is lost.

What Queue still asks us is `GET /internal/v1/admissions/{admission_id}/reservation` before
releasing a selection-timeout slot. It reports `CONFIRMED` too, so Queue can tell a buyer who
paid from one who walked away.

The rule enforced here is a five-ticket cap, not one purchase per person. If the product
wants "a buyer who has bought may not queue again", that is Queue declining to admit them;
this service would still let them hold up to their remaining allowance.

Error body (§10):

```json
{ "error": { "code": "HOLD_EXPIRED", "message": "Your reservation has expired." },
  "correlation_id": "uuid" }
```

| Status | Used for |
|---|---|
| 400 | malformed request — `INVALID_REQUEST` |
| 401 | missing/invalid authentication |
| 403 | invalid admission entitlement — `INVALID_ADMISSION`, `SELECTION_DEADLINE_PASSED` |
| 404 | `EVENT_NOT_FOUND`, `CATEGORY_NOT_FOUND`, `HOLD_NOT_FOUND` |
| 409 | business conflict — `INSUFFICIENT_INVENTORY`, `PURCHASE_LIMIT_EXCEEDED`, `HOLD_EXPIRED`, `EVENT_CANCELLED`, `ADMISSION_ALREADY_HAS_HOLD`, `ORDER_MISMATCH`, `IDEMPOTENCY_KEY_CONFLICT` |
| 503 | `DEPENDENCY_UNAVAILABLE` |

**Note there is no 410.** The contract folds expired/cancelled into 409.

## Endpoints owned by this service

Public (§4):

| Method | Path |
|---|---|
| GET | `/api/v1/events/{event_id}/availability` |
| POST | `/api/v1/reservations` |
| GET | `/api/v1/reservations/{hold_id}` |
| DELETE | `/api/v1/reservations/{hold_id}` *(an extension to §4)* |

Internal (§5):

| Method | Path | Caller |
|---|---|---|
| GET | `/internal/v1/events/{event_id}/capacity` | Queue |
| GET | `/internal/v1/admissions/{admission_id}/reservation` | Queue |
| GET | `/internal/v1/reservations/{hold_id}` | Checkout |
| POST | `/internal/v1/reservations/{hold_id}/confirm` | Checkout |
| POST | `/internal/v1/events/{event_id}/inventory` | tests; the same as `EventPublished` |
| POST | `/internal/v1/events/{event_id}/cancellation` | tests; the same as `EventCancelled` |

Plus `GET /health` for the ALB.

## Settings

```properties
reservation.hold-ttl-minutes=${HOLD_TTL_MINUTES:6}
reservation.payment-deadline-minutes=${PAYMENT_DEADLINE_MINUTES:5}
reservation.max-tickets-per-user=${MAX_TICKETS_PER_USER:5}
reservation.expiry-sweep-seconds=${EXPIRY_SWEEP_SECONDS:10}
reservation.reconcile-seconds=${RECONCILE_SECONDS:60}
reservation.admission-token-algorithm=${ADMISSION_TOKEN_ALGORITHM:RS256}
reservation.admission-token-secret=${ADMISSION_TOKEN_SECRET:}
reservation.admission-token-public-key=${ADMISSION_TOKEN_PUBLIC_KEY:}
reservation.rabbitmq-url=${RABBITMQ_URL:amqp://localhost:5672/%2F}
reservation.outbox-publish-seconds=${OUTBOX_PUBLISH_SECONDS:1}
```

**Queue signs admission tokens with its RSA private key and this service verifies with the
public half.** RS256 is therefore the default, and deployment supplies
`ADMISSION_TOKEN_PUBLIC_KEY` as PEM. We hold only a public key and could not mint a token
even by accident. The service **refuses to start** without the key material for whichever
algorithm is configured — coming up unable to verify tokens would be worse than not coming
up at all.

`scripts/dev-admission-keypair.sh` generates a throwaway pair for local runs and prints the
environment variables, so no key material is ever committed. The HS256 shared-secret path
remains in the code and is what the test suite signs with, which keeps a private key out of
the repository; the RS256 path is covered by `Rs256AdmissionTokenTest`, which generates a
keypair and signs with the private half.

The algorithm is chosen by configuration and never by the token's own `alg` header, which is
what stops a forged token asking to be checked a weaker way, or not at all.

`payment_expires_at` = created + 5 min (shown to the buyer). The Redis key lives **6 min** —
the extra minute is the contract's processing buffer, and it is what keeps the
confirm/expiry race unreachable in practice.

## Package structure

```
com.ticketing.reservation_service
├── config/        RedisScriptConfig, RedisClientConfig, CorrelationIdFilter, ClockConfig,
│                  ReservationProperties, GatewayHeaders, RabbitConfig
├── messaging/     EventMessageListener (messages in), OutboxPublisher (messages out)
├── controller/    HealthController, AvailabilityController, ReservationController,
│                  InternalReservationController, InternalAdmissionController,
│                  InternalEventController
├── service/       ReservationService, InventoryService, InventoryRebuildService,
│                  AdmissionTokenService, IdempotencyService, HoldExpiryWorker,
│                  EventCancellationService, OutboxWriter
├── repository/    HoldRepository, CategoryInventoryRepository, EventSaleStatusRepository,
│                  OutboxRepository, IdempotencyRepository, HoldRedisRepository,
│                  AvailabilityRedisRepository, RedisKeys
├── model/         Hold, HoldStatus, SaleStatus, CategoryInventory, CategoryInventoryId,
│                  EventSaleStatus, OutboxMessage, IdempotencyRecord
├── dto/           request bodies (CreateHoldRequest, ConfirmRequest, SeedInventoryRequest, ...),
│                  message payloads (EventPublishedData, EventCancelledData) and *View responses
└── exception/     ApiException, ErrorCode, GlobalExceptionHandler
```

No `JacksonConfig`: Spring Boot 4.1 ships Jackson 3, whose mapper is configured through
`spring.jackson.*` properties instead. `ErrorCode` binds each error code to its HTTP status
so no endpoint can pair the two wrongly; `RedisKeys` is the one place key names are built.

Lua scripts live in `src/main/resources/scripts/`: `hold.lua`, `reclaim.lua`, `confirm.lua`
and `return-pending.lua`.

---

## Phase 1 — Foundation

- [x] Add `spring-boot-starter-validation` to `pom.xml`.
- [x] Entities, **all IDs `UUID`/`CHAR(36)`**:
      `CategoryInventory` (event_id, category_id, total_quantity, sold_quantity,
      unit_amount, currency — **no held_quantity**), `Hold`, `EventSaleStatus`,
      `OutboxMessage`.
      `Hold` columns: hold_id, user_id, **user_sub**, event_id, category_id,
      admission_id **(unique)**, quantity, unit_amount, total_amount, currency, status,
      order_id, correlation_id, created_at, payment_expires_at, hold_expires_at.
      (`user_sub` was added in Phase 3: the gateway only ever tells us the Cognito id, so it
      is the only identity an incoming request carries and the only thing ownership checks
      can compare against.)
- [x] Jackson: snake_case, ISO-8601 instants, omit nulls, **no data envelope**.
- [x] `ApiException` + `GlobalExceptionHandler` emitting the §10 error body.
- [x] `CorrelationIdFilter` — read or generate `correlation_id`, put it in the MDC and on
      every response.
- [x] `GET /health`, and `GET /api/v1/events/{event_id}/availability` — `total` and `sold`
      from MySQL, `free` read straight off the Redis counter. Nothing is subtracted.
- [x] `POST /internal/v1/events/{event_id}/inventory` — stands in for the `EventPublished`
      consumer until Phase 9, seeding totals **and unit prices**. Idempotent. It must also
      set the Redis keys `avail:{e}:<cat>` and `sale:{e}`, or availability has nothing to
      read.

**Done when:** seed an event, then availability returns the right total/free/sold, and a
bad request returns the §10 error shape with a correlation_id.

## Phase 2 — Create a hold (the atomic core)

- [x] `scripts/hold.lua` — one script checking sale status, inventory initialised, the
      five-ticket limit and availability, then decrementing and registering the hold.
- [x] `RedisScriptConfig`, `HoldRedisRepository`.
- [x] `POST /api/v1/reservations` returning the §6 body: `hold_id`, `admission_id`,
      `status: ACTIVE`, `quantity`, `unit_amount`, `total_amount`, `currency`,
      `payment_expires_at`, `hold_expires_at`, `server_time`.
- [x] Price snapshot copied from `CategoryInventory` onto the hold (Event Service call
      arrives in Phase 9).
- [x] **`admission_id` comes from inside the admission token**, so this phase decodes the
      token's claims *without verifying the signature*. Phase 3 adds verification. Nothing
      is shipped between these two phases.
- [x] Redis first, then the MySQL row; if the insert fails, undo the Redis hold.
- [x] Errors: 409 `INSUFFICIENT_INVENTORY` / `PURCHASE_LIMIT_EXCEEDED` / `EVENT_CANCELLED`,
      404 `CATEGORY_NOT_FOUND`.

- [x] **A concurrency test now, not in Phase 8:** N threads racing for the last ticket,
      asserting exactly one success and no oversell. This is the guarantee the whole design
      exists for — it should be proven the moment the script exists, not eight phases later.

**Done when:** that test passes, two concurrent requests for the last ticket give exactly
one 201 and one 409, and a 6th ticket for one buyer gives 409 `PURCHASE_LIMIT_EXCEEDED`.

## Phase 3 — Admission enforcement

- [x] `AdmissionTokenService` — verify the signature, then the buyer, event and
      **selection deadline** (§6: an 11-minute token does not permit a hold after the
      5-minute selection deadline). RS256 or HS256, chosen by configuration and never by
      the token's `alg` header; `alg: none` and a mismatched `alg` are both refused, and the
      signature comparison is constant-time.
- [x] Buyer identity comes from `X-User-Sub`, set by API Gateway from the verified Cognito
      token (§3, and §6 of the Cognito notes). `user_id` was never added to
      `CreateHoldRequest`, so the public body already matches the contract.
- [x] Every refusal leaves inventory untouched, so a rejected token can never cost a
      ticket.
- [x] Unique constraint on `admission_id` — at most one hold per admission.
- [x] A used admission is refused **before** `hold.lua` runs, so a replayed token never
      takes tickets even briefly. The unique constraint stays as the backstop for two attempts
      that pass the check together.
- [x] 403 `INVALID_ADMISSION` / `SELECTION_DEADLINE_PASSED`;
      409 `ADMISSION_ALREADY_HAS_HOLD`.

**Done when:** a tampered or expired token is refused, and a second hold on the same
admission is refused.

## Phase 4 — Hold lifecycle

- [x] `GET /api/v1/reservations/{hold_id}` — Redis liveness decides `ACTIVE` vs expired;
      buyers see only their own holds (§3).
- [x] `DELETE /api/v1/reservations/{hold_id}` — `scripts/reclaim.lua` (ZREM as the
      atomic claim).
- [x] `HoldExpiryWorker` — `@Scheduled`, driven by the `zexp` sorted set.
      A due entry with no MySQL row is an orphan (Redis hold made, insert then failed):
      return its inventory and clean up. If its hold hash has expired too, the quantity is
      unknowable, so clear the entry and leave the counter to Phase 7's rebuild.

### Correction — ZREM is the claim, not the conditional UPDATE

This phase originally said the conditional `UPDATE` should guard inventory in the sweep.
That ordering leaks: `DELETE` wins the `UPDATE` to `CANCELLED`, the sweep then sees a
non-`ACTIVE` row and clears the index entry, and `DELETE`'s reclaim finds nothing left to
claim — so nobody returns the tickets.

So **`reclaim.lua`'s `ZREM` is the single claim**: whoever removes the member from the expiry
index owns that hold's ending, and Redis lets exactly one caller win. The winner alone
returns inventory and writes the final status; every loser is told `ALREADY_RECLAIMED` and
changes nothing. The conditional `UPDATE` remains as the second guard, so a final status is
written once and never overwritten by a slower caller.

**Anything added later that ends a hold must win this claim first**, Phase 5's confirmation
and Phase 10's cancellation included. A path that transitions a hold without claiming it
could return the same tickets twice.

A hold whose Redis key has already died is refused by `DELETE` with 409 `HOLD_NOT_ACTIVE`
rather than released: those tickets are the sweep's to return, and racing it is exactly how
a double refund would happen.

**Done when:** a hold expires on its own and its tickets come back; DELETE restores
availability immediately; neither path ever double-refunds.

## Phase 5 — Confirmation

- [x] `POST /internal/v1/reservations/{hold_id}/confirm` with `{order_id, payment_reference}`.
- [x] Requires the Redis key to still exist, else 409 `HOLD_EXPIRED` so Checkout refunds.
- [x] **Wins the Phase 4 claim before changing anything** (`ZREM` on the `zexp` member), so a
      confirmation and an expiry arriving together cannot both act. Winning the claim is also
      what stops the sweep expiring a hold that has just been paid for.
- [x] Conditional `UPDATE ... WHERE status='ACTIVE'` is the idempotency arbiter:
      same `order_id` again → 200; a different `order_id` → 409 `ORDER_MISMATCH` (§7
      "Reject and flag for reconciliation"). A duplicate is recognised from the stored row,
      not from the claim, since the first confirmation has already consumed it.
- [x] On success `sold_quantity` increases and availability stays put (held became sold).

**Done when:** confirming twice returns success both times with `sold_quantity` moving once.

## Phase 6 — Remaining internal endpoints

- [x] `GET /internal/v1/reservations/{hold_id}` — ownership, category, quantity,
      **price snapshot**, state, deadlines (what Checkout prices the order from).
- [x] `GET /internal/v1/events/{event_id}/capacity` — readiness, sale state,
      available/sold, plus **derived advisory** `held` clamped at zero.
- [x] `GET /internal/v1/admissions/{admission_id}/reservation` — lets Queue check before
      releasing a selection-timeout slot.

**Done when:** Queue and Checkout can be driven entirely through these routes with curl.

## Phase 7 — Idempotency, validation and resilience

- [x] `Idempotency-Key` on `POST /api/v1/reservations`: same key + same payload returns the
      original result **without extending deadlines**; same key + different payload →
      409 `IDEMPOTENCY_KEY_CONFLICT` (§6, §10).
- [x] `@Valid` plus `@NotNull` / `@Min(1)` / `@Max(5)` on request DTOs.
- [x] Rebuild Redis from MySQL when a counter is missing:
      `avail = total − sold − SUM(active, unexpired holds)`. **This is the only place that
      formula may appear.** Guard it so it cannot run while holds are being created.
      This is also the backstop for the two drifts earlier phases deliberately leave alone:
      an orphan whose hold hash expired before the sweep saw it (Phase 4), and a crash
      between a status transition committing and its reclaim running.

### Correction — the rebuild counts every ACTIVE hold, overdue ones included

The formula above said *unexpired* holds. That double-credits: an overdue ACTIVE hold has
not given its tickets back yet, so excluding it from `held` returns those tickets in the
counter, and then the restored expiry index lets the sweep return them a second time.

So the rebuild subtracts **every** `ACTIVE` hold, which is exactly what the counter looked
like before the loss, and the sweep then resolves the overdue ones as it always would.

The rebuild also restores what the rest of the lifecycle needs, or a Redis loss would
quietly break the guarantees around it:

- `userqty` per buyer, or the five-ticket limit resets and a buyer can take five more
- the hold keys of live holds, with **only the time they had left**, so a hold stays
  confirmable for no longer than it should have been
- the `zexp` entries of every `ACTIVE` hold, including overdue ones, because without them
  the sweep can never find those holds and their tickets are lost for good

**Write order is the guard.** No hold can be created while the availability counter is
missing — `hold.lua` refuses outright — so the counter is written **last**, after the
allowances and the expiry index. Until that final write the event is closed to new holds,
which is the quiet moment the rebuild needs. Counters are written with `SET NX`, so a
rebuild can never flatten one that live holds have already moved.

**Done when:** replaying the same create request returns the same hold and the same
deadlines; and after `redis-cli FLUSHALL`, availability and new holds are still correct.

## Phase 8 — Tests

- [x] Unit tests with Mockito for `ReservationService` and `InventoryService`.
      **Spring Boot 4 removed `@MockBean` — use `@MockitoBean`.**
- [x] `@SpringBootTest` integration tests against real Redis + MySQL, reading hosts from
      env vars so they work locally and in GitLab CI.
- [x] The §11 acceptance checks this service owns, as tests: two buyers cannot both take the
      last tickets; two tabs cannot bypass the five-ticket limit; retrying does not extend a
      deadline; confirmation racing expiry yields one outcome.

**Done when:** `./mvnw test` passes with `docker compose up -d` running. (Today the simplest
way is `docker compose --profile test run --rm test`, which also starts RabbitMQ.)

## Phase 9 — RabbitMQ and Event Service

- [x] Record the three messages another service consumes, one per sad path, each in the same
      transaction as the change it announces:
      - `ReservationExpired` → Checkout: an unpaid hold was swept by `HoldExpiryWorker`, so
        Checkout can drop its pending order instead of waiting for a payment that will fail
      - `RefundRequired` → Checkout: a paid hold's event was cancelled (Phase 10)
      - `EventSoldOut` → Queue: a hold took the event's last free ticket, so Queue stops
        admitting. It is filed under the event and keyed by that hold, so a second sell-out
        after tickets come back is a new message.

      A message written outside a request (the sweep, later the consumers) carries the
      correlation id of the request that created the hold.
- [x] ~~`ReservationCreated` / `ReservationConfirmed` / `ReservationCancelled`.~~ **Dropped for
      the MVP:** nobody consumes them. Each caller already gets the answer over HTTP: the buyer
      creates and releases the hold, Queue asks `GET /internal/v1/admissions/{id}/reservation`,
      and Checkout is the one calling `/confirm`. A cancelled event's unpaid holds are covered
      by `EventCancelled`. Removed from rabbitmq-service's `messages.yaml` as well.
- [x] Outbox publisher using the broker's envelope (`message_id`, `message_type`,
      `schema_version`, `occurred_at`, `producer`, `payload`) for the messages above.
- [x] Consume `EventPublished` (initialise inventory and prices) and `EventCancelled`,
      replacing the Phase 1 internal seeding endpoint (keep it for tests). Duplicate deliveries must be safe,
      and cancellation takes precedence over older messages.
- [x] ~~Replace the local price snapshot with
      `GET /internal/v1/events/{event_id}/categories/{category_id}`.~~ **Dropped for the MVP:**
      prices come only from `EventPublished` and are stored with the inventory. Calling Event
      Service on every hold would put it on the hottest path of a sale, so an Event outage
      would stop all holds.
- [x] Dead-letter queue for repeatedly failing messages.

### How it landed

The format was agreed on 2026-10-19 and lives in rabbitmq-service: `contracts/envelope.schema.json`
for the envelope, `topology/messages.yaml` for who sends what to whom, and `docs/MESSAGING.md`
for the rules every publisher and consumer follows. That repo owns the broker's topology. This
service **declares no queue, exchange or binding**; it connects, publishes and consumes.

- **Connecting.** `RABBITMQ_URL` (e.g. `amqp://ticketto:…@broker:5672/%2F`), the one variable
  every service is given; on AWS it comes from the `url` key of `rabibi/service/reservation-service`, this service's own
  least-privilege broker login.
  `RabbitConfig` parses it itself because Spring Boot does not decode it: it would take the
  vhost `%2F` literally rather than as `/`.
- **Publishing.** `OutboxPublisher` runs every second. It takes the oldest unsent rows
  (`FOR UPDATE SKIP LOCKED`, so a second instance takes different rows), wraps each in the
  envelope, and publishes it to this service's own exchange, `reservation.events`, with the
  message's dotted routing key from `messages.yaml` (`reservation.expired`, `refund.required`,
  `event.sold_out`): persistent, `mandatory`, and with `message_id`, `type` (the PascalCase
  name) and `correlation_id` set as AMQP properties. The correlation id is not in the envelope. A row is marked sent **only after the broker confirms it**. A failure leaves the
  row, counts an attempt, and stops the batch so the messages stay in order. A lost
  confirmation can mean a message goes out twice, never zero times; the stable `message_id` is
  there so consumers can deduplicate.
- **Consuming.** `EventMessageListener` listens on `reservation-service.EventPublished` and
  `reservation-service.EventCancelled`. Like a controller, it checks the message and calls
  the same service the internal routes call. Acks are sent after the work commits, never AMQP
  auto-ack (the broker's `ticketto-check` confirms this).
  - **Duplicates** are safe without a table of seen message ids, because both operations are
    already idempotent: seeding the same inventory again changes nothing, and a second
    cancellation finds nothing left to do.
  - **Out of order.** `EventCancelled` may overtake `EventPublished` because each type has its
    own queue. A cancellation for an unknown event is therefore *recorded* (sale status
    `CANCELLED`, no inventory). Any `EventPublished` arriving after a cancellation is
    acknowledged and ignored, so the event is never reopened.
- **Dead letters.** The broker owns the DLQs (`<queue>.dlq`). A message that can never work
  (not JSON, wrong type, missing fields, or inventory contradicting what is stored) is rejected
  straight there. Anything else, such as MySQL or Redis being down, is retried in-process
  three times, then dead-lettered. Replaying a dead letter is a manual step in the broker UI.
- **Boot 4 gotcha:** `docs/MESSAGING.md` gives `spring.rabbitmq.listener.simple.retry.max-attempts`,
  but Boot 4 removed that property and silently ignores it. This service sets `max-retries=2`
  instead. Other Spring services will hit the same thing.
- **Tests.** The test profile has no broker. `EventMessagesTest` hands the listeners messages
  shaped like `contracts/examples/*.json`, and `OutboxPublisherTest` drives the publisher against
  a stand-in `RabbitTemplate`. The whole path was also run against the real broker from
  rabbitmq-service's compose: publish → sell out (`queue-service.EventSoldOut`) → pay → cancel
  (`checkout-service.RefundRequired`) → a late `EventPublished` ignored → a poison message in the DLQ.
  Since then `BrokerIntegrationTest` runs against a real RabbitMQ in CI and in the compose `test`
  profile, loading the broker's topology itself.

### Open with other teams at the time

Kept for the record; the contract tests (After the plan) now pin all three payloads to
rabbitmq-service's schemas.

- **Checkout and `ReservationExpired`.** Checkout's handler (`POST /events/ReservationExpired`)
  is HTTP, not AMQP, and it requires `order_id`. An expired hold was never paid, so this
  service has no order id to send. Checkout stores `hold_id` on its orders and should match on
  that instead.
- **Checkout and `RefundRequired`.** Checkout is listed as its consumer but has no handler for
  it yet. Today it refunds every PAID order itself when it hears `EventCancelled`, which
  covers the same case.
- **Data schemas for our three messages.** rabbitmq-service has `contracts/` schemas only for
  Event Service's messages. Queue already expects `EventSoldOut {event_id, sold_out_at}`, which
  matches what we send. Adding schemas for `ReservationExpired`, `RefundRequired` and
  `EventSoldOut` would pin the other two down.
- **Queue treats `EventSoldOut` as final** (the queue closes for good). We send it again if
  returned tickets let the event sell out a second time; Queue reapplies it harmlessly but
  never reopens in between.

## Phase 10 — Event cancellation and refunds

Currently hidden inside one bullet, but System Design Scenario S3A specifies real logic and
`RefundRequired` is listed as an event **this service publishes** — nothing else in the plan
says when.

- [x] On cancellation: set `sale:{e}` to `CANCELLED` so `hold.lua` rejects new holds
      immediately, and mark `EventSaleStatus`.
- [x] Resolve each outstanding hold against the cancellation with the same conditional
      update used for expiry, so only one transition wins:
      - hold still `ACTIVE` → `CANCELLED`, release its inventory (no message: see Phase 9)
      - hold already `CONFIRMED` (the confirmation committed first) → leave it sold and
        publish **`RefundRequired`** with its `order_id`
- [x] Confirmations arriving after cancellation are rejected with 409 `EVENT_CANCELLED`, so
      Checkout records a refund obligation.
- [x] An older `EventPublished` must never reopen a cancelled event (§8 delivery rules).

**Done when:** cancelling an event mid-sale leaves every hold either released or sold-with-a-
refund-obligation, and never both.

### How it landed

- **Trigger:** the `EventCancelled` message, or `POST /internal/v1/events/{event_id}/cancellation`,
  which does the same by HTTP; both call `EventCancellationService`. Safe to repeat; a repeat
  reports zeros. Only the route answers 404 for an unknown event (see Phase 9).
- **Messages are recorded, then relayed.** `RefundRequired` goes into `outbox_message` in the
  same transaction as the change, holding only the message data; the Phase 9 publisher adds the
  envelope when it sends it. Message ids are derived from type + hold, so recording the same
  fact twice leaves one message.
- **The race the bullets above miss:** a confirmation can win the claim just before the
  cancellation reaches the hold, yet not have committed `CONFIRMED` when the cancellation reads
  the row. The cancellation then sees `ACTIVE`, has lost the claim, and moves on. So the
  confirmation re-reads the sale status *after* it commits and records the refund itself. Each
  side writes before it reads the other's write, so at least one sees the paid hold.
- A confirmation for a hold the cancellation already ended now answers `EVENT_CANCELLED`
  rather than `HOLD_EXPIRED`; both tell Checkout to refund, and that refund is Checkout's, so no
  `RefundRequired` is recorded for it.
- `outbox_message.payload` was `@Lob`, which MySQL maps to `tinytext` (255 bytes). It is now
  `TEXT`, and Flyway's `V1__baseline.sql` creates it that way.

## Phase 11 — Docker

- [x] Multi-stage `Dockerfile` (`maven:3.9-eclipse-temurin-21` → `eclipse-temurin:21-jre`).
- [x] `.dockerignore`; add the app to `docker-compose.yml` as a third service.
      It sits behind the `app` profile, so `docker compose up -d` still starts only Redis and
      MySQL for the tests: `docker compose --profile app up -d --build`.

## Phase 12 — CI/CD → AWS ECS Fargate

- [x] `.gitlab-ci.yml`: `lint` → `test` → `build` → `deploy`, beside GitLab's SAST, secret
      detection, dependency scanning and Terraform scanning.
- [x] `maven-tests` uses GitLab `services:` for Redis, MySQL and RabbitMQ; publishes JUnit and
      coverage reports.
- [x] `build-and-push` on `main`: pushes to ECR as `:<commit SHA>` (and `:latest`, used only as a
      build cache).
- [x] `deploy` on `main`, **automatic** rather than manual: fills `deploy/task-definition.json`,
      registers it, updates the service, waits for it to settle and fails if ECS rolled back.

### How it landed

- **AWS is an OpenTofu stack** in `deploy/terraform`: ECR, the security group, secrets, IAM
  roles (GitLab signs in with OIDC, no stored keys), the S3 env file and the ECS service on the
  team's `TicketPlatform` cluster. It only looks up what others own: the VPC, RDS, this service's
  Redis box on EC2 (System Design §8, so no `REDIS_SSL`) and the broker secret.
- **Public traffic** reaches it through the shared gateway stack: API Gateway → VPC link →
  internal ALB → target group `ticketto-reservation-api`. Checkout reaches it through Cloud Map
  at `reservation-service.ticketto.local:8081`.
- `deploy/README.md` is the runbook.

## After the plan

Added once the phases were done, each in its own merge request:

- **Flyway** owns the schema instead of `ddl-auto=update`; Hibernate only validates it.
  `V1__baseline.sql` is the schema as it was, `V2` adds the event and reconciliation indexes.
- **Reconciliation** (`HoldExpiryWorker.reconcile`, every `RECONCILE_SECONDS`): finishes holds
  whose MySQL write was lost or whose Redis state is gone, and returns orphaned tickets, never
  twice.
- **`EventSoldOut` only once every ticket is sold**, decided at confirmation, since a held ticket
  can still come back and Queue closes for good on it.
- **The admission token is tied to the buyer** presenting it (`user_id` = `X-User-Sub`).
- **Redis fails fast:** while it is unreachable, commands are refused at once and requests get
  503 `DEPENDENCY_UNAVAILABLE`, instead of hanging for the whole outage.
- **One bad item never stalls a loop:** the sweep and reconciliation skip a failing hold, and the
  outbox gives up on a message after 10 broker refusals (an outage counts none), leaving it
  unsent for a manual replay.
- **A failed confirmation keeps its claim** and re-checks cancellation on every retry, so a sale
  that may have committed is never handed back.
- **Contract tests** (`src/contract-test`, the `contract-tests` profile) check our three messages
  and our two consumers against rabbitmq-service's `ticketto-contracts` package, and run on every
  contract merge request that affects this service.
- **Lint:** Checkstyle and SpotBugs in CI.
- **Autoscaling:** 2 to 4 tasks on average CPU. The chaos experiments run against it live in the
  chaos-engineering repository.
