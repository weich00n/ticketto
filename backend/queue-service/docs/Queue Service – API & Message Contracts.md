# Queue Service – API & Message Contracts

Oct 8, 2026 · @Trevor

## Overview

The Queue service lets buyers wait for an event's sale and admits them, one per free slot, into an active pool of size max(1, floor(total\_capacity / 5)), issuing each admitted buyer an 11-minute signed admission token that Reservation validates.

Stack: FastAPI (HTTP API), a single admitter worker process, MySQL (queue configuration, one row per event, kept with a terminal status), Redis (waiting queue and active pool), RabbitMQ via pika (event-driven integration).

### Queue lifecycle

| Status | Entered when | Buyers can join | Admission |
| --- | --- | --- | --- |
| SCHEDULED | EventPublished received | No | No |
| OPEN | queue\_join\_opens\_at, as sent by Event | Yes, into an unordered pre-queue | No |
| ADMITTING | release\_at reached; pre-queue shuffled into FIFO order | Yes, appended in FIFO order | Yes, one buyer per free slot |
| CLOSED | EventSoldOut received from Reservation, or sale\_ends\_at reached | No | Stopped |
| CANCELLED | EventCancelled received from Event | No | Stopped |

The admitter derives OPEN and ADMITTING from the stored times on each tick, so no scheduled job is needed. The same applies to the end of the sale: once sale\_ends\_at (optional) passes, the queue counts as CLOSED, and the admitter persists it and clears the waiting queue exactly as for EventSoldOut. CLOSED and CANCELLED are terminal.

CLOSED covers two endings. Queue tells them apart by time: if sale\_ends\_at was reached before or when the queue closed, the sale **ended**; otherwise it **sold out**.

### Admission rules

- One waiting entry or one active admission per (user\_id, event\_id). Repeated joins or polls return the existing entry or admission and never start a new timer.
- Active-pool capacity = max(1, floor(total\_capacity / 5)), always derived, stored in the queue configuration (e.g. 304 tickets gives 60 slots; 3 tickets gives 1).
- At release, the admitter fills the pool up to capacity. After that, each slot released admits exactly one buyer from the head of the queue.
- An admission sets selection\_expires\_at = admitted\_at + 5 minutes and admission\_expires\_at = admitted\_at + 11 minutes. Holds last 6 minutes, so a hold created at the 5-minute mark expires at the 11-minute mark.
- Admitting a buyer is one atomic Redis operation: remove from the queue, record the admission with its deadlines, store the issued token.
- A single admitter instance runs at a time, so batches cannot race.

### When a slot is released

| Trigger | Source | Effect |
| --- | --- | --- |
| Order confirmed | OrderConfirmed from Checkout, carrying admission\_id | Admission removed; next buyer admitted immediately |
| 11-minute deadline | Admitter sweep on admission\_expires\_at | Admission removed; next buyer admitted |
| Event cancelled | EventCancelled from Event | Whole active pool and queue cleared; no further admission |
| Buyer asks again after the selection deadline | The admitted buyer's own GET …/queue/me or POST …/queue/entries after selection\_expires\_at | Admission removed; buyer appended to the back of the queue (not on a CLOSED or CANCELLED queue); next buyer admitted immediately. The frontend must stop polling once the buyer has a hold, or a buyer still checking out loses the admission. |

EventSoldOut does not remove active buyers: they stay until their order or deadline, but find no tickets to select. Waiting buyers are told the event is sold out.

Buyers may also leave the waiting queue voluntarily. Buyers who stop polling are not removed. A buyer whose admission has ended may join again at the back of the queue.

## Design recommendations

### Position updates: adaptive HTTP polling

Recommended: short HTTP polling, with the server telling each client when to poll next. It scales best for this service because:

- FastAPI instances stay stateless; no long-lived connections to hold open for tens of thousands of waiters.
- Each poll is one cheap Redis read (ZRANK is O(log n)), and polls are spread out by the server.
- The same call delivers the admission and token, so there is no second channel to keep in sync.
- It works through any load balancer or future API gateway without sticky sessions.

Every status response carries poll\_after\_seconds:

| Buyer state | poll\_after\_seconds |
| --- | --- |
| OPEN, before release | 30, capped at seconds until release\_at, plus 0 to 5 s random jitter |
| Waiting, position within active-pool capacity | 3 |
| Waiting, position up to 10 × capacity | 10 |
| Waiting, further back | 30 |
| Admitted, or queue CLOSED or CANCELLED | null (stop polling) |

The jitter spreads the spike of polls at release time. Server-Sent Events can be added later for buyers near the front without changing the contract.

### Admission token signing: RS256 with a JWKS endpoint

Recommended: an asymmetric key pair, RS256.

- Queue holds the private key and is the only service that can sign tokens.
- Reservation verifies with the public key, which Queue publishes at GET /.well-known/jwks.json. Reservation cannot forge tokens, unlike with a shared HS256 secret.
- Each token header carries a kid. Rotating keys means publishing the new key in JWKS, signing with it, and removing the old key after 11 minutes.
- In Python, PyJWT with the cryptography package covers both signing and verification.

## Admission token

The token is a JWT signed with RS256 whose claims are exactly the admission object; Reservation must reject a hold request after selection\_expires\_at.

Header:

```json
{ "alg": "RS256", "typ": "JWT", "kid": "queue-2026-10" }
```

Claims:

```json
{
  "admission_id": "8b0e5c1a-4f2d-4c1e-9a77-2d6f0b3e9c41",
  "user_id": "2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33",
  "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
  "admitted_at": "2026-11-20T12:00:00Z",
  "selection_expires_at": "2026-11-20T12:05:00Z",
  "admission_expires_at": "2026-11-20T12:11:00Z"
}
```

All timestamps are ISO 8601 in UTC. Queue issues the token once at admission and returns the same token string on every later poll.

### Reservation's validation rules

1. Fetch the public key from Queue's JWKS by kid (cache it; refetch on an unknown kid). Accept only alg RS256.
2. Verify the signature.
3. user\_id equals the authenticated buyer making the request.
4. event\_id equals the event in the request.
5. Creating a hold: current time is before selection\_expires\_at. The 11-minute lifetime never allows a hold after the 5-minute mark.
6. Store admission\_id with the reservation and pass it to Checkout, so OrderConfirmed can carry it back to Queue.

Tokens stay cryptographically valid until they expire, even if the event is cancelled. Reservation must check the event's own status before creating a hold.

## Buyer endpoints

Four endpoints cover the lobby: read the queue's schedule, join, poll for position or admission, and leave.

Base path: /api/v1. The buyer is identified by the X-User-Sub header (UUID) until an API gateway with JWT auth takes over. A missing or malformed header returns 401 UNAUTHENTICATED.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | /api/v1/events/{event\_id}/queue | Queue status and times for the lobby countdown |
| POST | /api/v1/events/{event\_id}/queue/entries | Join the queue (idempotent) |
| GET | /api/v1/events/{event\_id}/queue/me | Poll own position or admission |
| DELETE | /api/v1/events/{event\_id}/queue/me | Give up queue position |

### GET /api/v1/events/{event\_id}/queue

Public lobby information. No X-User-Sub required.

200 response:

```json
{
  "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
  "queue_status": "OPEN",
  "queue_opens_at": "2026-11-20T11:45:00Z",
  "release_at": "2026-11-20T12:00:00Z",
  "sale_ends_at": "2026-11-27T12:00:00Z",
  "server_time": "2026-11-20T11:52:13Z"
}
```

The client computes its countdown from server\_time, not the device clock. sale\_ends\_at is null when the event has no end time; when queue\_status is CLOSED and sale\_ends\_at ≤ server\_time, show "sales have ended" rather than "sold out". Errors: 404 EVENT\_NOT\_FOUND.

### POST /api/v1/events/{event\_id}/queue/entries

Joins the queue. No request body. Joining again returns the buyer's existing entry or admission unchanged.

- 201: new entry created.
- 200: buyer already waiting or admitted; existing state returned.

The response body has the same shape as GET /queue/me.

Errors:

- 404 EVENT\_NOT\_FOUND
- 409 QUEUE\_NOT\_OPEN (status SCHEDULED); body includes queue\_opens\_at
- 410 EVENT\_SOLD\_OUT (status CLOSED: sold out)
- 410 SALE\_ENDED (status CLOSED: sale\_ends\_at passed)
- 410 EVENT\_CANCELLED (status CANCELLED)

An admitted buyer who joins again after the queue closed gets 200 with their existing admission, whichever way it closed.

### GET /api/v1/events/{event\_id}/queue/me

The polling endpoint. Returns the buyer's waiting position, or their admission and token.

Waiting during OPEN, before release (order not yet decided):

```json
{
  "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
  "queue_status": "OPEN",
  "entry_status": "WAITING",
  "position": null,
  "release_at": "2026-11-20T12:00:00Z",
  "poll_after_seconds": 30
}
```

Waiting during ADMITTING (position is 1-based; 1 = next to be admitted):

```json
{
  "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
  "queue_status": "ADMITTING",
  "entry_status": "WAITING",
  "position": 142,
  "poll_after_seconds": 10
}
```

Admitted (same admission and token on every poll until it ends):

```json
{
  "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
  "queue_status": "ADMITTING",
  "entry_status": "ADMITTED",
  "admission": {
    "admission_id": "8b0e5c1a-4f2d-4c1e-9a77-2d6f0b3e9c41",
    "user_id": "2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33",
    "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
    "admitted_at": "2026-11-20T12:00:00Z",
    "selection_expires_at": "2026-11-20T12:05:00Z",
    "admission_expires_at": "2026-11-20T12:11:00Z"
  },
  "admission_token": "eyJhbGciOiJSUzI1NiIsImtpZCI6InF1ZXVlLTIwMjYtMTAifQ...",
  "poll_after_seconds": null
}
```

Not in the queue (never joined, left, or admission ended):

```json
{
  "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
  "queue_status": "ADMITTING",
  "entry_status": "NONE",
  "poll_after_seconds": null
}
```

When queue\_status is CLOSED or CANCELLED, waiting buyers get entry\_status NONE and the client shows "sold out", "sales have ended" (see the lobby's sale\_ends\_at) or "cancelled". An admitted buyer during CLOSED still gets their admission until it ends.

Errors: 404 EVENT\_NOT\_FOUND.

### DELETE /api/v1/events/{event\_id}/queue/me

Fired by the "give up queue position" button. Only a waiting buyer can leave; an admitted buyer's slot ends only by order or deadline.

- 204: entry removed.
- 404 NOT\_IN\_QUEUE
- 409 ALREADY\_ADMITTED

## Admin and internal endpoints

These endpoints are read-only views of queue state plus the key and health endpoints other services and infrastructure need; none of them changes the queue.

Base path: /internal/v1, reachable only inside the cluster network. How these endpoints are authenticated is an open point.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | /internal/v1/queues | List queue configurations, filterable by status |
| GET | /internal/v1/events/{event\_id}/queue | One queue's configuration and live counts |
| GET | /internal/v1/events/{event\_id}/queue/admissions | Active admissions for an event |
| GET | /internal/v1/admissions/{admission\_id} | Look up one admission |
| GET | /.well-known/jwks.json | Public keys for verifying admission tokens |
| GET | /health/live | Process is running |
| GET | /health/ready | MySQL, Redis and RabbitMQ are reachable |

### GET /internal/v1/queues

Query parameters: status (optional, one of SCHEDULED, OPEN, ADMITTING, CLOSED, CANCELLED), limit (default 50, max 200), cursor.

```json
{
  "items": [
    {
      "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
      "queue_status": "ADMITTING",
      "release_at": "2026-11-20T12:00:00Z"
    }
  ],
  "next_cursor": null
}
```

### GET /internal/v1/events/{event\_id}/queue

```json
{
  "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
  "queue_status": "ADMITTING",
  "event_version": 3,
  "total_capacity": 304,
  "active_pool_capacity": 60,
  "queue_opens_at": "2026-11-20T11:45:00Z",
  "release_at": "2026-11-20T12:00:00Z",
  "sale_ends_at": "2026-11-27T12:00:00Z",
  "closed_at": null,
  "waiting_count": 4821,
  "active_count": 60
}
```

waiting\_count and active\_count are read live from Redis. Errors: 404 EVENT\_NOT\_FOUND.

### GET /internal/v1/events/{event\_id}/queue/admissions

Query parameters: limit (default 50, max 200), cursor. Returns active admissions, oldest first.

```json
{
  "items": [
    {
      "admission_id": "8b0e5c1a-4f2d-4c1e-9a77-2d6f0b3e9c41",
      "user_id": "2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33",
      "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
      "admitted_at": "2026-11-20T12:00:00Z",
      "selection_expires_at": "2026-11-20T12:05:00Z",
      "admission_expires_at": "2026-11-20T12:11:00Z"
    }
  ],
  "next_cursor": null
}
```

### GET /internal/v1/admissions/{admission\_id}

Returns one admission object (the token is never returned here). 404 ADMISSION\_NOT\_FOUND once the admission has ended.

### GET /.well-known/jwks.json

```json
{
  "keys": [
    { "kty": "RSA", "kid": "queue-2026-10", "use": "sig", "alg": "RS256", "n": "0vx7agoebGcQSuu...", "e": "AQAB" }
  ]
}
```

Called by Reservation; safe to cache for several minutes.

### Health

/health/live returns 200 while the process runs. /health/ready returns 200 when MySQL, Redis and the RabbitMQ connection are up, otherwise 503 with the failing dependency named.

## RabbitMQ topology and envelope

Each producing service owns one durable topic exchange, and Queue consumes through one queue per message type (as set up on the shared broker, 2026-10-21), so order confirmations never wait behind event messages.

### Exchanges

| Exchange (topic, durable) | Owner | Routing keys |
| --- | --- | --- |
| event.events | Event service | event.published, event.updated, event.cancelled |
| reservation.events | Reservation service | event.sold\_out |
| checkout.events | Checkout service | order.confirmed |
| queue.events | Queue service | admission.granted, admission.released (optional, see below) |

### Queue service's consumer queues

| Queue (quorum, durable) | Bound to | Routing key |
| --- | --- | --- |
| queue-service.EventPublished | event.events | event.published |
| queue-service.EventCancelled | event.events | event.cancelled |
| queue-service.EventSoldOut | reservation.events | event.sold\_out |
| queue-service.OrderConfirmed | checkout.events | order.confirmed |

There is no queue for EventUpdated, so Queue doesn't receive it. Routing keys above are what Queue declares locally; on the shared broker the broker team owns the bindings.

Each queue has a dead-letter exchange ticketto.dlx routing to a matching .dlq queue. A message that fails processing 5 times is dead-lettered for inspection.

### Delivery rules

- Producers publish persistent messages (delivery\_mode 2) with publisher confirms.
- Queue acks manually, only after the MySQL and Redis changes commit. Prefetch: 50.
- Delivery is at least once, so every handler is idempotent: message\_id is recorded in a processed\_messages table, and event messages are also ordered by event\_version.
- With pika, run each consumer on its own BlockingConnection in a dedicated thread or process, separate from the FastAPI workers.

### Message envelope

Every message body uses this envelope, content type application/json. The AMQP properties message\_id and type repeat the envelope's message\_id and message\_type.

```json
{
  "message_id": "5d3c2b1a-7e8f-4a90-b1c2-d3e4f5a6b7c8",
  "message_type": "EventPublished",
  "schema_version": 1,
  "occurred_at": "2026-10-08T07:00:00Z",
  "producer": "event-service",
  "payload": { }
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| message\_id | UUID | Unique per message; used for deduplication |
| message\_type | string | Contract name, e.g. OrderConfirmed |
| schema\_version | integer | Incremented on a breaking payload change |
| occurred\_at | ISO 8601 UTC | When the fact happened at the producer |
| producer | string | Name of the publishing service |
| payload | object | Message-specific fields, defined below |

## Messages Queue consumes

Queue consumes five messages: three from Event, EventSoldOut from Reservation, and OrderConfirmed from Checkout. Payloads below go inside the envelope's payload field.

### EventPublished (Event → Queue)

Exchange event.events, routing key event.published.

```json
{
  "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
  "event_version": 1,
  "total_capacity": 304,
  "queue_join_opens_at": "2026-11-20T11:45:00Z",
  "release_at": "2026-11-20T12:00:00Z",
  "starts_at": "2026-11-27T12:00:00Z"
}
```

queue\_join\_opens\_at is **required**: when buyers can start joining the queue (into the unordered pre-queue). It must not be later than release\_at. Queue stores it as queue\_opens\_at.

starts\_at is the event's start time, which is also when ticket sales end. It is **optional** (omit it or send null for "no end"); if present it must be later than release\_at. Queue stores it as sale\_ends\_at. A message breaking that is malformed and is dead-lettered. The field is additive, so schema\_version stays 1.

Queue's handling:

1. If a row for event\_id exists with event\_version ≥ 1, ack and ignore (duplicate).
2. Insert the configuration row: status SCHEDULED, queue\_opens\_at = queue\_join\_opens\_at, active\_pool\_capacity = max(1, floor(total\_capacity / 5)), sale\_ends\_at = starts\_at as sent.
3. No Redis keys are created yet; they appear when the first buyer joins.

### EventUpdated (Event → Queue)

Exchange event.events, routing key event.updated. Carries the current values of every field Queue uses, not only the changed ones.

```json
{
  "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
  "event_version": 2,
  "total_capacity": 304,
  "queue_join_opens_at": "2026-11-21T11:45:00Z",
  "release_at": "2026-11-21T12:00:00Z",
  "starts_at": "2026-11-28T12:00:00Z"
}
```

Queue's handling:

1. If event\_version ≤ the stored version, ack and ignore (stale or duplicate).
2. Status SCHEDULED: update release\_at, queue\_opens\_at (from queue\_join\_opens\_at), total\_capacity, active\_pool\_capacity and sale\_ends\_at from starts\_at (an omitted or null starts\_at clears it, since the message carries full current values); store the new version.
3. Status OPEN or ADMITTING: reject any change, including to starts\_at, since the configuration is frozen once the lobby opens. Ack, log, leave the row unchanged. Reporting the error back to Event is out of scope for now.
4. Status CLOSED or CANCELLED: ack and ignore.

### EventCancelled (Event → Queue)

Exchange event.events, routing key event.cancelled.

```json
{
  "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
  "event_version": 3,
  "cancelled_at": "2026-11-19T09:30:00Z"
}
```

Queue's handling:

1. Set status CANCELLED and closed\_at in MySQL; the row is kept.
2. Stop admission for the event.
3. Delete the event's waiting queue and active pool from Redis. Polling buyers get entry\_status NONE with queue\_status CANCELLED.

### EventSoldOut (Reservation → Queue)

Exchange reservation.events, routing key event.sold\_out.

```json
{
  "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
  "sold_out_at": "2026-11-20T12:07:42Z"
}
```

Queue's handling:

1. If status is already CLOSED or CANCELLED, ack and ignore.
2. Set status CLOSED and closed\_at in MySQL; the row is kept.
3. Stop admission and delete the waiting queue from Redis. Waiting buyers get entry\_status NONE with queue\_status CLOSED, shown as sold out.
4. Leave the active pool alone. Its admissions end by order or deadline; the Redis keys expire after the last admission\_expires\_at.

### OrderConfirmed (Checkout → Queue)

Exchange checkout.events, routing key order.confirmed.

```json
{
  "order_id": "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b",
  "admission_id": "8b0e5c1a-4f2d-4c1e-9a77-2d6f0b3e9c41",
  "user_id": "2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33",
  "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
  "confirmed_at": "2026-11-20T12:06:15Z"
}
```

Queue's handling:

1. Look up the admission by admission\_id. If it no longer exists (already expired or released), ack and ignore.
2. If user\_id or event\_id does not match the stored admission, ack, log as an integrity error, and change nothing.
3. Remove the admission from the active pool and immediately admit the next buyer at the head of the queue, if the queue is ADMITTING.

## Messages Queue publishes (optional)

No other service needs a message from Queue for the purchase flow to work: the admission token reaches the buyer through polling, and Reservation trusts the token's signature. Two messages are proposed for audit and analytics; they can be added later without changing anything above.

### AdmissionGranted

Exchange queue.events, routing key admission.granted. Payload = the admission object.

```json
{
  "admission_id": "8b0e5c1a-4f2d-4c1e-9a77-2d6f0b3e9c41",
  "user_id": "2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33",
  "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
  "admitted_at": "2026-11-20T12:00:00Z",
  "selection_expires_at": "2026-11-20T12:05:00Z",
  "admission_expires_at": "2026-11-20T12:11:00Z"
}
```

### AdmissionReleased

Exchange queue.events, routing key admission.released.

```json
{
  "admission_id": "8b0e5c1a-4f2d-4c1e-9a77-2d6f0b3e9c41",
  "user_id": "2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33",
  "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
  "reason": "ORDER_CONFIRMED",
  "released_at": "2026-11-20T12:06:15Z"
}
```

reason is one of ORDER\_CONFIRMED, ADMISSION\_EXPIRED, EVENT\_CANCELLED.

## Errors and open points

### Error format

Every error response uses one body shape:

```json
{
  "error": {
    "code": "QUEUE_NOT_OPEN",
    "message": "The queue for this event opens at 2026-11-20T11:45:00Z.",
    "details": { "queue_opens_at": "2026-11-20T11:45:00Z" }
  }
}
```

| HTTP | Code | When |
| --- | --- | --- |
| 401 | UNAUTHENTICATED | X-User-Sub missing or not a UUID |
| 404 | EVENT\_NOT\_FOUND | No queue configuration for event\_id |
| 404 | NOT\_IN\_QUEUE | Leaving without a waiting entry |
| 404 | ADMISSION\_NOT\_FOUND | Admission ended or never existed |
| 409 | QUEUE\_NOT\_OPEN | Joining while status is SCHEDULED |
| 409 | ALREADY\_ADMITTED | Leaving the queue while admitted |
| 410 | EVENT\_SOLD\_OUT | Joining while status is CLOSED because the event sold out |
| 410 | SALE\_ENDED | Joining while status is CLOSED because sale\_ends\_at passed |
| 410 | EVENT\_CANCELLED | Joining while status is CANCELLED |
| 503 | SERVICE\_UNAVAILABLE | Redis or MySQL unreachable |

### Open points

- [ ] How a rejected EventUpdated is reported back to Event (out of scope for now).
- [ ] Authentication for /internal/v1 endpoints: network restriction only, or a service token.
- [ ] Spec section 3 says Queue checks for an existing reservation before a selection-timeout release. Per the agreed design, Queue never checks for a reservation and ReservationCreated is not consumed: a buyer past the selection deadline is requeued only when they poll or join again, and the frontend stops polling once the buyer has a hold. Remove that paragraph from the spec.
- [ ] Reservation must reject holds for a cancelled event even when the buyer's token is still valid.
