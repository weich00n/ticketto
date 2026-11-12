# Manual happy path with Postman

Step-by-step manual test of the Queue Service on the local dev stack: from an event being published to a buyer holding an admission token that Reservation accepts, then completing the order so the next buyer is admitted. Each step lists the request to send in Postman and what you should get back.

The Event, Reservation and Checkout services don't exist yet, so you **play them** by publishing their messages through the **RabbitMQ management API** (also a plain HTTP request in Postman).

**Scenario:** the event has `total_capacity` 5, so the **active pool holds 1 buyer**. Buyer 1 joins before release and is admitted at release. Buyer 2 joins after release and waits at position 1. When Buyer 1's order is confirmed, Buyer 2 is admitted immediately.

Total time: about 5 minutes, most of it waiting for the release.

---

## 0. Setup

### Start the stack

```bash
make keys       # only if secrets/jwt_private.pem doesn't exist yet
make up-app     # MySQL, Redis, RabbitMQ + migrate, api, admitter, consumer
docker compose --profile app ps                              # wait until all show "healthy"
docker compose --profile app logs -f api admitter consumer   # optional: watch the logs mentioned below
```

### Postman variables

Create these as collection (or environment) variables. The IDs can be any UUIDs; the example values work as they are.

| Variable | Value | Notes |
| --- | --- | --- |
| `baseUrl` | `http://localhost:8000` | Queue Service API |
| `rabbitUrl` | `http://localhost:15672` | RabbitMQ management API |
| `eventId` | `c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90` | Use a new UUID each time you rerun the walkthrough |
| `buyer1` | `2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33` | |
| `buyer2` | `7d3e9a41-0b5c-4f2e-9c8d-1a6b5e4f3c21` | |
| `releaseAt` | e.g. `2026-10-17T09:42:00Z` | **About 2 minutes from now, in UTC.** Set it just before step 1. |
| `queueJoinOpensAt` | e.g. `2026-10-17T09:27:00Z` | Any time up to `releaseAt`; 15 minutes before it opens the queue straight away |
| `saleEndsAt` | e.g. `2026-10-17T10:42:00Z` | Any time after `releaseAt`, e.g. one hour later |
| `admissionId1` | *(empty)* | Copy it from step 7's response |

Current UTC time: `date -u +%Y-%m-%dT%H:%M:%SZ` in a terminal.

For the three **RabbitMQ requests** (steps 1, 15 and 18), set **Authorization → Basic Auth**, username `queue`, password `queue`. All bodies are **raw → JSON**.

---

## Step 0a – Readiness

```
GET {{baseUrl}}/health/ready
```

**Expect `200`:**

```json
{ "status": "ready" }
```

The API can reach MySQL and Redis. A `503` names what's down, e.g. `{"status": "unavailable", "failed": ["redis"]}`. Fix that first.

---

## Step 1 – Event service publishes the event

```
POST {{rabbitUrl}}/api/exchanges/%2F/event.events/publish
Authorization: Basic queue / queue
```

**Body:**

```json
{
  "properties": {
    "content_type": "application/json",
    "delivery_mode": 2,
    "type": "EventPublished"
  },
  "routing_key": "event.published",
  "payload_encoding": "string",
  "payload": "{\"message_id\": \"{{$guid}}\", \"message_type\": \"EventPublished\", \"schema_version\": 1, \"occurred_at\": \"{{$isoTimestamp}}\", \"producer\": \"postman\", \"payload\": {\"event_id\": \"{{eventId}}\", \"event_version\": 1, \"total_capacity\": 5, \"queue_join_opens_at\": \"{{queueJoinOpensAt}}\", \"release_at\": \"{{releaseAt}}\", \"starts_at\": \"{{saleEndsAt}}\"}}"
}
```

The `payload` string is the message itself, in Queue's envelope format. Unescaped, it reads:

```json
{
  "message_id": "<new uuid>",
  "message_type": "EventPublished",
  "schema_version": 1,
  "occurred_at": "<now>",
  "producer": "postman",
  "payload": {
    "event_id": "{{eventId}}",
    "event_version": 1,
    "total_capacity": 5,
    "queue_join_opens_at": "{{queueJoinOpensAt}}",
    "release_at": "{{releaseAt}}",
    "starts_at": "{{saleEndsAt}}"
  }
}
```

**Expect `200`:**

```json
{ "routed": true }
```

- `"routed": true` means the message reached Queue's consumer queue. `false` means the consumer isn't running or hasn't declared its queues.
- **Consumer log:** `message handled … message_type=EventPublished outcome=created`.
- **What happened:** a queue config was stored with status `SCHEDULED` and an active pool capacity of 1 (5 ÷ 5). The lobby opens at `queueJoinOpensAt`, which has already passed, so within a second the **admitter logs `queue opened`** and the status becomes `OPEN`.

---

## Step 2 – Lobby info (anyone, no login)

```
GET {{baseUrl}}/api/v1/events/{{eventId}}/queue
```

**Expect `200`:**

```json
{
  "event_id": "{{eventId}}",
  "queue_status": "OPEN",
  "queue_opens_at": "{{queueJoinOpensAt}}",
  "release_at": "{{releaseAt}}",
  "sale_ends_at": "{{saleEndsAt}}",
  "server_time": "<now>"
}
```

- `OPEN` means buyers can join, but nobody is admitted until `release_at`.
- Clients count down using `server_time`, never the device clock.
- A `404 EVENT_NOT_FOUND` means step 1 hasn't been processed yet: resend after a second.

---

## Step 3 – Operator view of the new queue

```
GET {{baseUrl}}/internal/v1/events/{{eventId}}/queue
```

**Expect `200`:**

```json
{
  "event_id": "{{eventId}}",
  "queue_status": "OPEN",
  "event_version": 1,
  "total_capacity": 5,
  "active_pool_capacity": 1,
  "queue_opens_at": "…",
  "release_at": "{{releaseAt}}",
  "sale_ends_at": "{{saleEndsAt}}",
  "closed_at": null,
  "waiting_count": 0,
  "active_count": 0
}
```

The stored configuration, plus live counts read from Redis.

---

## Step 4 – Buyer 1 joins (before release)

```
POST {{baseUrl}}/api/v1/events/{{eventId}}/queue/entries
X-User-Sub: {{buyer1}}
```

No body.

**Expect `201`:**

```json
{
  "event_id": "{{eventId}}",
  "queue_status": "OPEN",
  "entry_status": "WAITING",
  "position": null,
  "release_at": "{{releaseAt}}",
  "poll_after_seconds": 32
}
```

- Buyer 1 is in the **unordered pre-queue**. `position` is `null` because the order is decided by a random shuffle at release.
- `poll_after_seconds` is about 30 (never more than the seconds left until release) plus 0–5 seconds of random jitter.

---

## Step 5 – Buyer 1 joins again (idempotent)

Send step 4 again, unchanged.

**Expect `200`** (not 201), with the same body. Joining twice never creates a second entry or changes the buyer's place.

---

## Step 6 – Buyer 1 polls before release

```
GET {{baseUrl}}/api/v1/events/{{eventId}}/queue/me
X-User-Sub: {{buyer1}}
```

**Expect `200`:** the same shape as step 4 (`OPEN`, `WAITING`, `position: null`).

Now **wait until `releaseAt`**, plus a couple of seconds. At release:

- The **admitter logs** `pre-queue shuffled … ordered=1` and `admit … admitted=1`.
- The status becomes `ADMITTING`.

---

## Step 7 – Buyer 1 is admitted

Send step 6 again after the release.

**Expect `200`:**

```json
{
  "event_id": "{{eventId}}",
  "queue_status": "ADMITTING",
  "entry_status": "ADMITTED",
  "admission": {
    "admission_id": "<uuid>",
    "user_id": "{{buyer1}}",
    "event_id": "{{eventId}}",
    "admitted_at": "<release time>",
    "selection_expires_at": "<admitted_at + 5 min>",
    "admission_expires_at": "<admitted_at + 11 min>"
  },
  "admission_token": "eyJhbGciOiJSUzI1NiIs…",
  "poll_after_seconds": null
}
```

- **Copy `admission.admission_id` into the `admissionId1` variable** for later steps.
- `poll_after_seconds: null` tells the client to stop polling.
- `selection_expires_at` (5 minutes after admission) is the deadline for Reservation to let Buyer 1 create a hold.
- `admission_expires_at` (11 minutes after admission) is when the admission ends if no order is confirmed.
- If you still get `WAITING`, the admitter hasn't run since release yet: resend after a second.

**Also try:** send it again. The `admission_token` is **identical** every time, and the deadlines don't move.

---

## Step 8 – Public keys (JWKS)

```
GET {{baseUrl}}/.well-known/jwks.json
```

**Expect `200`**, with the response header `Cache-Control: public, max-age=300`:

```json
{
  "keys": [
    { "kty": "RSA", "kid": "queue-local-1", "use": "sig", "alg": "RS256", "n": "…", "e": "AQAB" }
  ]
}
```

These are the keys Reservation uses to verify admission tokens.

---

## Step 9 – Check the token as Reservation would

Paste Buyer 1's `admission_token` from step 7 into [jwt.io](https://jwt.io). It's a local test token, so that's fine; never paste production tokens into external sites. Paste the matching key from step 8 as the public key to verify the signature.

What Reservation checks (from "Admission token contract (for Reservation)" in `docs/Endpoint reference.md`):

| # | Check | What you should see |
| --- | --- | --- |
| 1 | Header `alg` | `RS256` |
| 2 | Header `kid` is in the JWKS | `queue-local-1`, the same as step 8 |
| 3 | Signature | "Signature verified" with the step 8 key |
| 4 | Claims | Exactly six: `admission_id`, `user_id`, `event_id`, `admitted_at`, `selection_expires_at`, `admission_expires_at` (no `exp` or `iat`) |
| 5 | `user_id` | `{{buyer1}}` |
| 6 | `event_id` | `{{eventId}}` |
| 7 | Current time is before `selection_expires_at` | Within 5 minutes of step 7 |

If every row matches, Buyer 1 is **"into Reservation"**: Reservation would accept the token, create a hold, and store the `admission_id`.

---

## Step 10 – Operator view: active admissions

```
GET {{baseUrl}}/internal/v1/events/{{eventId}}/queue/admissions
```

**Expect `200`:**

```json
{
  "items": [
    {
      "admission_id": "{{admissionId1}}",
      "user_id": "{{buyer1}}",
      "event_id": "{{eventId}}",
      "admitted_at": "…",
      "selection_expires_at": "…",
      "admission_expires_at": "…"
    }
  ],
  "next_cursor": null
}
```

One active admission, oldest first.

---

## Step 11 – Look up the admission by id

```
GET {{baseUrl}}/internal/v1/admissions/{{admissionId1}}
```

**Expect `200`:** the six admission fields, as in step 10. This endpoint never includes the token.

---

## Step 12 – Buyer 1 can't leave once admitted

```
DELETE {{baseUrl}}/api/v1/events/{{eventId}}/queue/me
X-User-Sub: {{buyer1}}
```

**Expect `409`:**

```json
{ "error": { "code": "ALREADY_ADMITTED", "message": "…", "details": {} } }
```

An admitted buyer's slot ends only by a confirmed order or the 11-minute deadline.

---

## Step 13 – Buyer 2 joins after release

```
POST {{baseUrl}}/api/v1/events/{{eventId}}/queue/entries
X-User-Sub: {{buyer2}}
```

No body.

**Expect `201`:**

```json
{
  "event_id": "{{eventId}}",
  "queue_status": "ADMITTING",
  "entry_status": "WAITING",
  "position": 1,
  "poll_after_seconds": 3
}
```

- After release, joiners go to the **back of a strict first-in-first-out queue** and get a real `position`.
- `poll_after_seconds` is 3 because Buyer 2 is near the front.
- Buyer 2 isn't admitted yet: the pool (capacity 1) is full with Buyer 1.

---

## Step 14 – Buyer 2 polls: still waiting

```
GET {{baseUrl}}/api/v1/events/{{eventId}}/queue/me
X-User-Sub: {{buyer2}}
```

**Expect `200`:** `WAITING`, `position: 1`. A slot only frees up when Buyer 1's order is confirmed or Buyer 1's 11 minutes run out.

---

## Step 15 – Checkout confirms Buyer 1's order

```
POST {{rabbitUrl}}/api/exchanges/%2F/checkout.events/publish
Authorization: Basic queue / queue
```

**Body:**

```json
{
  "properties": {
    "content_type": "application/json",
    "delivery_mode": 2,
    "type": "OrderConfirmed"
  },
  "routing_key": "order.confirmed",
  "payload_encoding": "string",
  "payload": "{\"message_id\": \"{{$guid}}\", \"message_type\": \"OrderConfirmed\", \"schema_version\": 1, \"occurred_at\": \"{{$isoTimestamp}}\", \"producer\": \"postman\", \"payload\": {\"order_id\": \"{{$guid}}\", \"admission_id\": \"{{admissionId1}}\", \"user_id\": \"{{buyer1}}\", \"event_id\": \"{{eventId}}\", \"confirmed_at\": \"{{$isoTimestamp}}\"}}"
}
```

**Expect `200`:** `{ "routed": true }`

- **Consumer log:** `message handled … message_type=OrderConfirmed outcome=released`.
- **What happened:** Buyer 1's admission was removed, freeing the slot. The consumer then **immediately** admitted the next buyer, without waiting for the admitter's next tick.

---

## Step 16 – Buyer 1's admission has ended

```
GET {{baseUrl}}/api/v1/events/{{eventId}}/queue/me
X-User-Sub: {{buyer1}}
```

**Expect `200`:**

```json
{
  "event_id": "{{eventId}}",
  "queue_status": "ADMITTING",
  "entry_status": "NONE",
  "poll_after_seconds": null
}
```

Also send:

```
GET {{baseUrl}}/internal/v1/admissions/{{admissionId1}}
```

**Expect `404`:** `{ "error": { "code": "ADMISSION_NOT_FOUND", … } }`

> **Known gap (KIV in the plan):** Buyer 1's old token would still pass step 9's checks until its `selection_expires_at`. Queue can't revoke a token it has issued. The agreed direction is for Reservation to refuse a new hold for an `admission_id` that already has a confirmed order.

---

## Step 17 – Buyer 2 has been admitted

```
GET {{baseUrl}}/api/v1/events/{{eventId}}/queue/me
X-User-Sub: {{buyer2}}
```

**Expect `200`:** `"entry_status": "ADMITTED"`, with Buyer 2's own `admission` (`user_id` = `{{buyer2}}`) and `admission_token`.

**End of the happy path:** one buyer was admitted, ordered and released, and the next buyer was admitted in their place.

---

## Step 18 (optional) – Reservation reports the event sold out

```
POST {{rabbitUrl}}/api/exchanges/%2F/reservation.events/publish
Authorization: Basic queue / queue
```

**Body:**

```json
{
  "properties": {
    "content_type": "application/json",
    "delivery_mode": 2,
    "type": "EventSoldOut"
  },
  "routing_key": "event.sold_out",
  "payload_encoding": "string",
  "payload": "{\"message_id\": \"{{$guid}}\", \"message_type\": \"EventSoldOut\", \"schema_version\": 1, \"occurred_at\": \"{{$isoTimestamp}}\", \"producer\": \"postman\", \"payload\": {\"event_id\": \"{{eventId}}\", \"sold_out_at\": \"{{$isoTimestamp}}\"}}"
}
```

**Expect `200`:** `{ "routed": true }`. **Consumer log:** `outcome=closed`.

Then:

| Request | Expect |
| --- | --- |
| Step 2 (lobby) | `"queue_status": "CLOSED"` |
| Step 17 (Buyer 2 polls) | Still `ADMITTED`, now with `"queue_status": "CLOSED"`. Admitted buyers keep their admission until order or deadline. |
| Step 13 with a **new** `X-User-Sub` (e.g. `{{$guid}}`) | `410` `{ "error": { "code": "EVENT_SOLD_OUT", … } }` |
| Step 3 (operator view) | `"queue_status": "CLOSED"`, `closed_at` set, `"waiting_count": 0` |

---

## Clean up

```bash
make down    # stop everything (MySQL data is kept); add `docker compose down -v` to wipe it
```

## Status codes you'll meet

| Code | Where |
| --- | --- |
| 200 | Lobby, polls, repeat join, operator views, RabbitMQ publishes |
| 201 | First join of each buyer (steps 4 and 13) |
| 204 | Leaving while waiting: try step 12 with a buyer who's still `WAITING` |
| 401 `UNAUTHENTICATED` | Any buyer request without a valid `X-User-Sub` header |
| 404 `EVENT_NOT_FOUND` | Lobby or join before step 1 has been processed |
| 404 `ADMISSION_NOT_FOUND` | Admission lookup after the order (step 16) |
| 409 `ALREADY_ADMITTED` | Admitted buyer tries to leave (step 12) |
| 410 `EVENT_SOLD_OUT` | New buyer after step 18 |
