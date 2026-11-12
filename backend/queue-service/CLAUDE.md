# Queue Service

Waiting-room service for a ticketing platform. Buyers join a per-event queue; a single admitter moves them, FIFO, into a capped **active pool** whenever a slot frees up, and issues each admitted buyer a signed admission token that the Reservation service validates.

Full spec (source of truth for contracts): "docs/Queue Service – API & Message Contracts.md" doc, with endpoint details in "docs/Endpoint reference.md". This file summarises it plus implementation decisions.

## Tech stack

- Python, **FastAPI** (HTTP API)
- **MySQL**: queue configuration (one row per event, never deleted; terminal statuses kept)
- **Redis**: waiting queues, active pools, admissions (runtime state only)
- **RabbitMQ** via **pika**: event-driven integration. Broker not yet provisioned; we propose the contracts below.
- **PyJWT + cryptography**: RS256 admission tokens

## Processes

Three separately run processes sharing the same codebase:

1. **api**: FastAPI app (uvicorn). Stateless; can scale horizontally.
2. **admitter**: single instance only (no horizontal scaling for now, avoids races). Tick loop, ~1 s.
3. **consumer**: pika consumers for RabbitMQ messages (each consumer on its own BlockingConnection/thread or process).

## Domain rules (do not change without asking)

### Lifecycle

| Status | Entered when | Join allowed | Admission |
| --- | --- | --- | --- |
| SCHEDULED | EventPublished received | No (409 QUEUE_NOT_OPEN) | No |
| OPEN | `queue_opens_at` (sent by Event as `queue_join_opens_at`, ≤ `release_at`) | Yes, into unordered pre-queue | No |
| ADMITTING | `release_at` reached; pre-queue shuffled | Yes, appended FIFO | Yes |
| CLOSED | EventSoldOut (from Reservation), or `sale_ends_at` reached | No (410 EVENT_SOLD_OUT, or 410 SALE_ENDED if `sale_ends_at` ≤ when it closed) | Stopped |
| CANCELLED | EventCancelled (from Event) | No (410 EVENT_CANCELLED) | Stopped |

- OPEN/ADMITTING are **derived from time** by a shared helper `effective_status(config, now)`; no scheduler or delayed job. The admitter also persists transitions to MySQL so admin listing/filtering works.
- `sale_ends_at` (optional, sent as `starts_at` in EventPublished/EventUpdated; null = no end, must be > `release_at`): once passed, `effective_status` returns CLOSED. The admitter persists CLOSED with `closed_at = sale_ends_at` and runs the same cleanup as EventSoldOut (waiting cleared, admitted buyers keep their admission until order or deadline). Decided 2026-10-17: reuse CLOSED, distinct join error SALE_ENDED.
- CLOSED and CANCELLED are terminal. Rows are kept, never deleted.
- Use **server time** only (prefer Redis `TIME` inside scripts). Never trust client clocks.

### Capacity and admission

- `active_pool_capacity = max(1, floor(total_capacity / 5))`. Always derived; stored in the config row. E.g. 304 → 60, 3 → 1.
- At release: fill the pool up to capacity. After that, every slot released admits exactly one buyer from the head of the queue.
- One waiting entry **or** one active admission per `(user_id, event_id)`. Repeated joins/polls return existing state and never start a new timer.
- Admission deadlines: `selection_expires_at = admitted_at + 5 min`, `admission_expires_at = admitted_at + 11 min`. (Holds last 6 min, so the last possible hold expires at the 11-minute mark.)
- Pre-release joiners (OPEN) are **randomly shuffled** at release; after that strictly FIFO by join order.
- Multiple events can be OPEN/ADMITTING concurrently; all state is per event. A buyer may be in several events' queues at once.

### Slot release: only these triggers

1. **OrderConfirmed** (from Checkout, carries `admission_id`) → remove admission, immediately admit next buyer.
2. **11-minute deadline** (`admission_expires_at`) → removed by admitter sweep.
3. **EventCancelled** → whole active pool and queue cleared.
4. **Admitted buyer polls or joins again after `selection_expires_at`** (`GET /queue/me` or `POST /queue/entries`) → admission removed (`requeue_lapsed.lua`), buyer appended to the back of the waiting queue, next buyer admitted immediately. On a CLOSED/CANCELLED queue the buyer is evicted but not requeued. Decided 2026-10-30. This relies on the **frontend stopping polling once the buyer has a hold**: Queue doesn't know about holds, so a buyer who polls (refresh, second tab) after the 5-minute mark loses the admission even mid-checkout.

Not triggers: hold expiry (Queue does not consume hold messages), ReservationCreated (not consumed), the selection deadline passing on its own (only the buyer's own poll/join acts on it), EventSoldOut (active buyers stay until order or deadline; waiting buyers are cleared and see "sold out"). Admitted buyers cannot leave voluntarily.

### Waiting queue exits

- Voluntary leave via `DELETE /queue/me` (waiting buyers only; 409 ALREADY_ADMITTED if admitted).
- Buyers who stop polling are **not** removed.
- After an admission ends, a buyer may join again at the back.

### Event config changes

- EventUpdated applies only while SCHEDULED. While OPEN or ADMITTING, **any** change is rejected (ack, log, leave row unchanged). Reporting rejection back to Event is out of scope.
- Ignore messages with `event_version <=` stored version.

## Redis design

Single Redis for now. All per-event keys use a hash tag `{event_id}` so a future Redis Cluster keeps an event's keys in one slot (required for multi-key Lua scripts).

| Key | Type | Contents |
| --- | --- | --- |
| `queue:live_events` | SET | event_ids currently OPEN or ADMITTING (admitter's work list) |
| `queue:{eid}:config` | HASH | cached config: release_at, queue_opens_at, capacity, status, event_version |
| `queue:{eid}:prequeue` | SET | user_ids who joined during OPEN (unordered) |
| `queue:{eid}:shuffled` | STRING | flag set once at release (`SET NX`); guards shuffle running twice |
| `queue:{eid}:seq` | STRING | INCR counter for FIFO order |
| `queue:{eid}:waiting` | ZSET | user_id → seq |
| `queue:{eid}:active` | ZSET | admission_id → admission_expires_at (ms) |
| `queue:{eid}:user_adm` | HASH | user_id → admission_id |
| `queue:{eid}:adm:{admission_id}` | HASH | admission fields (admission_id, user_id, event_id, admitted_at, selection_expires_at, admission_expires_at) |
| `queue:admission_index` | HASH | admission_id → event_id (for `GET /internal/v1/admissions/{id}`; written by Python after the script, not inside it, to stay cluster-safe) |

Put a long safety TTL (e.g. a few days after release) on per-event keys so failed cleanup never leaks.

### Lua scripts (atomic; live in the repo, registered with `redis.register_script`)

Nothing is configured on the Redis server. Store scripts as `.lua` files, load with redis-py `register_script` (uses EVALSHA, auto-reloads).

- **join**: if user has an admission or is already in prequeue/waiting → return existing state. Else if `shuffled` flag set → `INCR seq`, `ZADD waiting`; otherwise `SADD prequeue`. (Status checks for SCHEDULED/CLOSED/CANCELLED happen in Python before calling.)
- **shuffle** (once per event at release): Python reads prequeue, shuffles with `secrets.SystemRandom`, passes the ordered list. Script sets `shuffled` flag (abort if already set), assigns seqs in that order via INCR + ZADD for members still in prequeue, appends any leftover prequeue members (joined mid-shuffle), then DELs prequeue. Do not shuffle inside Lua (script randomness is not reliable).
- **admit** (called by admitter tick and by OrderConfirmed consumer):
  1. sweep: for every admission in `active` with score ≤ now, delete its hash, its `user_adm` entry and its ZSET member; return released ids.
  2. `free = capacity - ZCARD active`; if > 0, `ZPOPMIN waiting free`.
  3. for each popped user: take an admission_id from a list of UUIDs Python pre-generated and passed as ARGV (Lua has no UUID), write the admission hash, `user_adm`, and `ZADD active`.
  4. return admitted and released admissions. Use `redis.call('TIME')` for now.
- **release** (OrderConfirmed): if admission_id not in `active` → return not-found (ignore). If stored user_id/event_id mismatch → return mismatch (log integrity error, change nothing). Else remove admission hash, `user_adm`, ZSET member.
- **leave**: if user in `user_adm` → ALREADY_ADMITTED; else remove from waiting or prequeue; else NOT_IN_QUEUE.

The admit script's capacity check + pop is atomic, so even concurrent callers cannot overfill the pool or double-admit.

### Status / polling lookup (`GET /queue/me`)

1. `user_adm` has user → ADMITTED: read admission hash, sign token (see below), `poll_after_seconds: null`. If now > `selection_expires_at`, requeue instead (slot release trigger 4) and answer from the new state.
2. In `waiting` → WAITING, `position = ZRANK + 1`.
3. In `prequeue` → WAITING, `position: null`.
4. Else → `entry_status: NONE`, `poll_after_seconds: null`.

`poll_after_seconds`:

| State | Value |
| --- | --- |
| OPEN, before release | 30, capped at seconds until release_at, plus 0–5 s random jitter |
| Waiting, position ≤ capacity | 3 |
| Waiting, position ≤ 10 × capacity | 10 |
| Waiting, further back | 30 |
| Admitted / CLOSED / CANCELLED / NONE | null |

## Admitter worker

Plain Python process (async), single instance:

```python
async def run_admitter():
    while True:
        started = time.monotonic()
        for eid in await live_event_ids():          # SMEMBERS queue:live_events
            try:
                await process_event(eid)            # isolate failures per event
            except Exception:
                log.exception("admitter failed for %s", eid)
        await asyncio.sleep(max(0, 1.0 - (time.monotonic() - started)))

async def process_event(eid):
    cfg = await get_config(eid)                     # from Redis cache
    now = server_now()
    if now < cfg.release_at:
        return                                      # OPEN: nothing to admit
    if not cfg.shuffled:
        await run_shuffle(eid)                      # also persists ADMITTING to MySQL
    await run_admit(eid, cfg.active_pool_capacity)
```

- Add an event to `queue:live_events` when it reaches OPEN (admitter or a cheap check on events whose `queue_opens_at` is near), remove on CLOSED/CANCELLED.
- Sequential processing is fine; switch to `asyncio.gather` only if ticks get slow.
- Future scaling (not now): multiple admitters with per-event lease locks `SET queue:{eid}:lock <worker> NX PX 5000`.

## Admission token

- JWT, **RS256**. Queue holds the private key; public keys served at `GET /.well-known/jwks.json`. Header includes `kid`.
- Claims are exactly the admission object, no others: `admission_id, user_id, event_id, admitted_at, selection_expires_at, admission_expires_at` (ISO 8601 UTC strings).
- Not stored: signed on demand from the admission hash on each poll. RS256 (PKCS#1 v1.5) is deterministic, so every poll returns the identical token.
- Key rotation: publish new key in JWKS, sign with it, remove old key after 11 minutes.
- Reservation (not us) validates: signature via JWKS by kid, alg RS256 only, user_id = caller, event_id = request event, now < selection_expires_at to create a hold; it stores admission_id and passes it to Checkout. Reservation must also reject holds for cancelled events even with a valid token.

## HTTP API

Error body everywhere: `{"error": {"code": "<CODE>", "message": "<text>", "details": {}}}`.

Buyer identity: `X-User-Sub` header (UUID) for now; an API gateway with JWT auth may replace it later, so keep identity extraction in one dependency. Missing/invalid → 401 UNAUTHENTICATED.

### Buyer (`/api/v1`)

| Method | Path | Success | Errors |
| --- | --- | --- | --- |
| GET | `/api/v1/events/{event_id}/queue` | 200 `{event_id, queue_status, queue_opens_at, release_at, server_time}` (no X-User-Sub needed) | 404 EVENT_NOT_FOUND |
| POST | `/api/v1/events/{event_id}/queue/entries` | 201 new / 200 existing; body same shape as `/queue/me` | 401, 404 EVENT_NOT_FOUND, 409 QUEUE_NOT_OPEN (details.queue_opens_at), 410 EVENT_SOLD_OUT, 410 EVENT_CANCELLED |
| GET | `/api/v1/events/{event_id}/queue/me` | 200 `{event_id, queue_status, entry_status: WAITING\|ADMITTED\|NONE, position?, release_at?, admission?, admission_token?, poll_after_seconds}` | 401, 404 EVENT_NOT_FOUND |
| DELETE | `/api/v1/events/{event_id}/queue/me` | 204 | 401, 404 NOT_IN_QUEUE, 409 ALREADY_ADMITTED |

### Internal (`/internal/v1`, cluster-network only; auth TBD)

| Method | Path | Success | Errors |
| --- | --- | --- | --- |
| GET | `/internal/v1/queues?status=&limit=50&cursor=` | 200 `{items: [{event_id, queue_status, release_at}], next_cursor}` (limit max 200) | — |
| GET | `/internal/v1/events/{event_id}/queue` | 200 `{event_id, queue_status, event_version, total_capacity, active_pool_capacity, queue_opens_at, release_at, closed_at, waiting_count, active_count}` | 404 EVENT_NOT_FOUND |
| GET | `/internal/v1/events/{event_id}/queue/admissions?limit=&cursor=` | 200 `{items: [admission], next_cursor}`, oldest first | 404 EVENT_NOT_FOUND |
| GET | `/internal/v1/admissions/{admission_id}` | 200 admission object (never the token) | 404 ADMISSION_NOT_FOUND |
| GET | `/.well-known/jwks.json` | 200 `{keys: [{kty, kid, use, alg, n, e}]}` | — |
| GET | `/health/live` | 200 `{"status": "ok"}` | — |
| GET | `/health/ready` | 200 `{"status": "ready"}` | 503 `{"status": "unavailable", "failed": ["redis"]}` |

Any endpoint may return 503 SERVICE_UNAVAILABLE when Redis/MySQL are down. Full example bodies are in the doc's "Endpoint reference" tab.

## RabbitMQ

### Topology

| Exchange (topic, durable) | Owner | Routing keys |
| --- | --- | --- |
| `event.events` | Event | `event.published`, `event.updated`, `event.cancelled` |
| `reservation.events` | Reservation | `event.sold_out` |
| `checkout.events` | Checkout | `order.confirmed` |
| `queue.events` | Queue | `admission.granted`, `admission.released` (optional) |

Queue's consumer queues (quorum, durable), each with DLX `ticketto.dlx` → matching `.dlq`; dead-letter after 5 failed attempts. The consumer enforces the 5 attempts itself (reject without requeue on the 5th failure), because the shared broker's queues have `delivery-limit: -1` (unlimited). Failed messages wait 1, 2, 4, then 8 s before redelivery (held unacked on a timer), and are always given back with `basic_reject`: on RabbitMQ 4.3 `basic_nack(requeue=True)` doesn't advance `x-delivery-count`, so they would loop forever:

| Queue | Bound to | Key |
| --- | --- | --- |
| `queue-service.EventPublished` | `event.events` | `event.published` |
| `queue-service.EventCancelled` | `event.events` | `event.cancelled` |
| `queue-service.EventSoldOut` | `reservation.events` | `event.sold_out` |
| `queue-service.OrderConfirmed` | `checkout.events` | `order.confirmed` |

One queue per message type, named as on the shared broker (2026-10-21). The bindings are what we declare locally; on the shared broker the broker team owns them. **EventUpdated is not consumed**: there is no queue for it (decided 2026-10-21), so changes to an event's `release_at`, `total_capacity` or `sale_ends_at` after EventPublished don't reach Queue. The handler stays, so adding a queue later is a one-line change in `topology.py`.

Locally (`RABBITMQ_DECLARE_TOPOLOGY=true`, the default) the consumer declares all of these idempotently at startup, exchanges too. On the shared broker (`false`) it declares nothing: the broker team creates our queues, DLQs and bindings, and the consumer only checks its four queues exist and subscribes to them. Decided 2026-10-21.

Rules: persistent messages + publisher confirms; manual ack only after MySQL/Redis changes commit; prefetch 50; at-least-once delivery so handlers are idempotent: event messages are ordered by `event_version` with forward-only status rules, and OrderConfirmed for an already-released admission is a no-op. Message IDs are not stored (`processed_messages` was dropped on 2026-10-21; it was only an audit record).

### Envelope

```json
{
  "message_id": "uuid",
  "message_type": "EventPublished",
  "schema_version": 1,
  "occurred_at": "2026-10-08T07:00:00Z",
  "producer": "event-service",
  "payload": {}
}
```

AMQP `message_id` and `type` properties mirror the envelope; content type `application/json`.

### Consumed messages and handling

**EventPublished** `{event_id, event_version, total_capacity, queue_join_opens_at, release_at, starts_at?}`
`starts_at` is the event's start time, which is also when ticket sales end; it is stored as `sale_ends_at` (decided 2026-10-28).
Duplicate (row exists, version ≥) → ignore. Else insert row: SCHEDULED, `queue_opens_at = queue_join_opens_at` (required, must be ≤ `release_at`; decided 2026-10-28, replacing the fixed 15-minute lead), `active_pool_capacity = max(1, floor(total_capacity / 5))`; cache config. No queue keys yet.

**EventUpdated** `{event_id, event_version, total_capacity, queue_join_opens_at, release_at, starts_at?}` (full current values; omitted/null `starts_at` clears `sale_ends_at`)
Version ≤ stored → ignore. SCHEDULED → update fields, recompute derived ones, refresh cache. OPEN/ADMITTING → reject any change (ack, log). CLOSED/CANCELLED → ignore.

**EventCancelled** `{event_id, event_version, cancelled_at}`
Set CANCELLED + closed_at (keep row). Remove from `live_events`. Delete all the event's Redis queue/pool/admission keys.

**EventSoldOut** (from Reservation) `{event_id, sold_out_at}`
Already terminal → ignore. Set CLOSED + closed_at. Remove from `live_events`; delete prequeue and waiting. Keep active pool; let its keys expire after the last `admission_expires_at`.

**OrderConfirmed** (from Checkout) `{order_id, admission_id, user_id, event_id, confirmed_at}`
Run release script. Not found → ignore. Mismatch → log integrity error, change nothing. Released → run admit script for that event immediately (if ADMITTING).

### Published (optional, can come later)

- `AdmissionGranted`: payload = admission object.
- `AdmissionReleased`: `{admission_id, user_id, event_id, reason: ORDER_CONFIRMED|ADMISSION_EXPIRED|EVENT_CANCELLED, released_at}`.

## MySQL schema (proposed)

```sql
CREATE TABLE queue_configs (
  event_id             CHAR(36)     PRIMARY KEY,
  event_version        INT          NOT NULL,
  total_capacity       INT          NOT NULL,
  active_pool_capacity INT          NOT NULL,
  queue_opens_at       DATETIME(3)  NOT NULL,
  release_at           DATETIME(3)  NOT NULL,
  sale_ends_at         DATETIME(3)  NULL,
  status               ENUM('SCHEDULED','OPEN','ADMITTING','CLOSED','CANCELLED') NOT NULL,
  closed_at            DATETIME(3)  NULL,
  created_at           DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at           DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  INDEX idx_status_release (status, release_at),
  INDEX idx_status_opens (status, queue_opens_at)
);
```

All timestamps stored and returned in UTC.

## Suggested layout

```
app/
  main.py                 # FastAPI app factory
  api/buyer.py            # /api/v1 routes
  api/internal.py         # /internal/v1, jwks, health
  api/deps.py             # current_user (X-User-Sub), db/redis deps
  core/config.py          # settings from env
  core/db.py  core/redis.py
  core/tokens.py          # RS256 signing, JWKS
  core/status.py          # effective_status(), poll_after_seconds()
  queue/repository.py     # wrappers around Lua scripts and Redis reads
  queue/lua/*.lua         # join, shuffle, admit, release, leave
  messaging/topology.py   # declare exchanges/queues/bindings
  messaging/consumers.py  # pika consumers + handlers
  messaging/schemas.py    # pydantic models for envelope + payloads
workers/admitter.py       # tick loop entrypoint
workers/consumer.py       # consumer entrypoint
migrations/
tests/
```

Env vars: `MYSQL_URL`, `REDIS_URL`, `RABBITMQ_URL`, `JWT_PRIVATE_KEY_PATH`, `JWT_KID`, `ADMITTER_TICK_SECONDS` (default 1).

## Testing priorities

Test against a real Redis (Docker, via `docker-compose.test.yml`), not mocks, since correctness lives in the Lua scripts.

- Pool never exceeds capacity under concurrent admit calls (tick + OrderConfirmed racing).
- Idempotent join/poll: same admission, same token, no new timer.
- Shuffle runs exactly once; joins during the shuffle are not lost.
- Expiry sweep frees slots and admits the next buyer FIFO.
- OrderConfirmed for unknown/expired admission is a no-op; mismatch changes nothing.
- EventUpdated rejected while OPEN/ADMITTING; stale versions ignored.
- SoldOut keeps active admissions; Cancelled clears everything.
- Two events admitting simultaneously stay independent.

## Open points (ask before deciding)

- How a rejected EventUpdated is reported back to Event (out of scope for now).
- Authentication for `/internal/v1` endpoints: network restriction only, or service token.
- Original spec section 3's "check for existing reservation before selection-timeout release" is superseded: remove from spec; do not implement.
- Global cap across all events' active pools (if Reservation has a total load limit) is not designed yet.
