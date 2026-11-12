# Endpoint reference

The Queue service exposes 11 HTTP endpoints: 4 for buyers and 7 internal. All bodies are JSON (application/json) unless a row says otherwise.

The admission token that buyers receive, and how the Reservation service must validate it, is specified in [Admission token contract (for Reservation)](#admission-token-contract-for-reservation) at the end of this tab.

Every error uses `{"error": {"code": "<CODE>", "message": "<text>", "details": {}}}`; details is omitted below except where it carries data. The health response bodies are newly specified here; the main tab only gave their status codes.

## Buyer endpoints

### 1. Get queue info

| Field | Value |
| --- | --- |
| Endpoint | GET /api/v1/events/{event\_id}/queue |
| Parameters | Path: event\_id (UUID, required). No X-User-Sub needed. |
| Request body | None |
| Response type | application/json |
| 200 OK | `{"event_id": "c4a1e8f2-…", "queue_status": "OPEN", "queue_opens_at": "2026-11-20T11:45:00Z", "release_at": "2026-11-20T12:00:00Z", "sale_ends_at": "2026-11-27T12:00:00Z", "server_time": "2026-11-20T11:52:13Z"}`. sale\_ends\_at is null when the event has no end time. |
| 404 Not Found | `{"error": {"code": "EVENT_NOT_FOUND", "message": "…"}}` |
| 503 Service Unavailable | `{"error": {"code": "SERVICE_UNAVAILABLE", "message": "…"}}` |

### 2. Join the queue

| Field | Value |
| --- | --- |
| Endpoint | POST /api/v1/events/{event\_id}/queue/entries |
| Parameters | Path: event\_id (UUID, required). Header: X-User-Sub (UUID, required). |
| Request body | None |
| Response type | application/json |
| 201 Created | New entry. Same shape as endpoint 3, e.g. `{"event_id": "c4a1e8f2-…", "queue_status": "OPEN", "entry_status": "WAITING", "position": null, "release_at": "2026-11-20T12:00:00Z", "poll_after_seconds": 30}` |
| 200 OK | Already waiting or admitted. Existing state, same shapes as endpoint 3's 200 responses. |
| 401 Unauthorized | `{"error": {"code": "UNAUTHENTICATED", "message": "…"}}` |
| 404 Not Found | `{"error": {"code": "EVENT_NOT_FOUND", "message": "…"}}` |
| 409 Conflict | `{"error": {"code": "QUEUE_NOT_OPEN", "message": "…", "details": {"queue_opens_at": "2026-11-20T11:45:00Z"}}}` |
| 410 Gone | `{"error": {"code": "EVENT_SOLD_OUT", "message": "…"}}` (sold out), `{"error": {"code": "SALE_ENDED", "message": "…"}}` (sale\_ends\_at passed) or `{"error": {"code": "EVENT_CANCELLED", "message": "…"}}`. Not returned to a buyer who still holds an admission: they get 200 with it. |
| 503 Service Unavailable | `{"error": {"code": "SERVICE_UNAVAILABLE", "message": "…"}}` |

### 3. Poll own status

| Field | Value |
| --- | --- |
| Endpoint | GET /api/v1/events/{event\_id}/queue/me |
| Parameters | Path: event\_id (UUID, required). Header: X-User-Sub (UUID, required). |
| Request body | None |
| Response type | application/json |
| 200 OK, waiting before release | `{"event_id": "c4a1e8f2-…", "queue_status": "OPEN", "entry_status": "WAITING", "position": null, "release_at": "2026-11-20T12:00:00Z", "poll_after_seconds": 30}` |
| 200 OK, waiting while admitting | `{"event_id": "c4a1e8f2-…", "queue_status": "ADMITTING", "entry_status": "WAITING", "position": 142, "poll_after_seconds": 10}` |
| 200 OK, admitted (token format: see [Admission token contract](#admission-token-contract-for-reservation)) | `{"event_id": "c4a1e8f2-…", "queue_status": "ADMITTING", "entry_status": "ADMITTED", "admission": {"admission_id": "8b0e5c1a-…", "user_id": "2f9c7d10-…", "event_id": "c4a1e8f2-…", "admitted_at": "2026-11-20T12:00:00Z", "selection_expires_at": "2026-11-20T12:05:00Z", "admission_expires_at": "2026-11-20T12:11:00Z"}, "admission_token": "eyJhbGciOiJSUzI1NiIs…", "poll_after_seconds": null}` |
| 200 OK, not in queue | `{"event_id": "c4a1e8f2-…", "queue_status": "CLOSED", "entry_status": "NONE", "poll_after_seconds": null}` |
| 401 Unauthorized | `{"error": {"code": "UNAUTHENTICATED", "message": "…"}}` |
| 404 Not Found | `{"error": {"code": "EVENT_NOT_FOUND", "message": "…"}}` |
| 503 Service Unavailable | `{"error": {"code": "SERVICE_UNAVAILABLE", "message": "…"}}` |

**After the selection deadline:** if an admitted buyer calls this endpoint (or POST …/queue/entries) after `selection_expires_at`, the admission ends and the buyer goes to the back of the queue; the response is the new state (usually `WAITING` with a position, or `ADMITTED` with a new admission if nobody else was waiting). On a CLOSED or CANCELLED queue the buyer gets `NONE`. **The frontend must stop polling once the buyer has created a hold**, since Queue can't tell whether a buyer is mid-checkout.

### 4. Leave the queue

| Field | Value |
| --- | --- |
| Endpoint | DELETE /api/v1/events/{event\_id}/queue/me |
| Parameters | Path: event\_id (UUID, required). Header: X-User-Sub (UUID, required). |
| Request body | None |
| Response type | No body on success; application/json on error |
| 204 No Content | No body |
| 401 Unauthorized | `{"error": {"code": "UNAUTHENTICATED", "message": "…"}}` |
| 404 Not Found | `{"error": {"code": "NOT_IN_QUEUE", "message": "…"}}` |
| 409 Conflict | `{"error": {"code": "ALREADY_ADMITTED", "message": "…"}}` |
| 503 Service Unavailable | `{"error": {"code": "SERVICE_UNAVAILABLE", "message": "…"}}` |

## Internal endpoints

### 5. List queues

| Field | Value |
| --- | --- |
| Endpoint | GET /internal/v1/queues |
| Parameters | Query: status (optional: SCHEDULED, OPEN, ADMITTING, CLOSED, CANCELLED; filters on the stored status, which can lag the time-derived `queue_status` by about a second), limit (optional, default 50; values outside 1–200 are clamped), cursor (optional, the previous page's `next_cursor`). |
| Request body | None |
| Response type | application/json |
| 200 OK | `{"items": [{"event_id": "c4a1e8f2-…", "queue_status": "ADMITTING", "release_at": "2026-11-20T12:00:00Z"}], "next_cursor": null}`. Ordered by release_at, then event_id. |
| 400 Bad Request | Unknown status, non-numeric limit, or malformed cursor. `{"error": {"code": "INVALID_REQUEST", "message": "…", "details": {"errors": [{"loc": ["query", "cursor"], "message": "…"}]}}}` |
| 503 Service Unavailable | `{"error": {"code": "SERVICE_UNAVAILABLE", "message": "…"}}` |

### 6. Get queue configuration and counts

| Field | Value |
| --- | --- |
| Endpoint | GET /internal/v1/events/{event\_id}/queue |
| Parameters | Path: event\_id (UUID, required). |
| Request body | None |
| Response type | application/json |
| 200 OK | `{"event_id": "c4a1e8f2-…", "queue_status": "ADMITTING", "event_version": 3, "total_capacity": 304, "active_pool_capacity": 60, "queue_opens_at": "2026-11-20T11:45:00Z", "release_at": "2026-11-20T12:00:00Z", "sale_ends_at": "2026-11-27T12:00:00Z", "closed_at": null, "waiting_count": 4821, "active_count": 60}` |
| 404 Not Found | `{"error": {"code": "EVENT_NOT_FOUND", "message": "…"}}` |
| 503 Service Unavailable | `{"error": {"code": "SERVICE_UNAVAILABLE", "message": "…"}}` |

### 7. List active admissions

| Field | Value |
| --- | --- |
| Endpoint | GET /internal/v1/events/{event\_id}/queue/admissions |
| Parameters | Path: event\_id (UUID, required). Query: limit (optional, default 50; values outside 1–200 are clamped), cursor (optional). |
| Request body | None |
| Response type | application/json |
| 200 OK | Oldest first. `{"items": [{"admission_id": "8b0e5c1a-…", "user_id": "2f9c7d10-…", "event_id": "c4a1e8f2-…", "admitted_at": "2026-11-20T12:00:00Z", "selection_expires_at": "2026-11-20T12:05:00Z", "admission_expires_at": "2026-11-20T12:11:00Z"}], "next_cursor": null}` |
| 400 Bad Request | Non-numeric limit or malformed cursor: `INVALID_REQUEST`, as for endpoint 5. |
| 404 Not Found | `{"error": {"code": "EVENT_NOT_FOUND", "message": "…"}}` |
| 503 Service Unavailable | `{"error": {"code": "SERVICE_UNAVAILABLE", "message": "…"}}` |

### 8. Get one admission

| Field | Value |
| --- | --- |
| Endpoint | GET /internal/v1/admissions/{admission\_id} |
| Parameters | Path: admission\_id (UUID, required). |
| Request body | None |
| Response type | application/json |
| 200 OK | Token never included. `{"admission_id": "8b0e5c1a-…", "user_id": "2f9c7d10-…", "event_id": "c4a1e8f2-…", "admitted_at": "2026-11-20T12:00:00Z", "selection_expires_at": "2026-11-20T12:05:00Z", "admission_expires_at": "2026-11-20T12:11:00Z"}` |
| 404 Not Found | Admission ended or never existed. `{"error": {"code": "ADMISSION_NOT_FOUND", "message": "…"}}` |
| 503 Service Unavailable | `{"error": {"code": "SERVICE_UNAVAILABLE", "message": "…"}}` |

### 9. Get token public keys

| Field | Value |
| --- | --- |
| Endpoint | GET /.well-known/jwks.json |
| Parameters | None |
| Request body | None |
| Response type | application/json, sent with `Cache-Control: public, max-age=300`. How Reservation uses it: see [Admission token contract](#admission-token-contract-for-reservation). |
| 200 OK | `{"keys": [{"kty": "RSA", "kid": "queue-2026-10", "use": "sig", "alg": "RS256", "n": "0vx7agoebGcQSuu…", "e": "AQAB"}]}` |

### 10. Liveness check

| Field | Value |
| --- | --- |
| Endpoint | GET /health/live |
| Parameters | None |
| Request body | None |
| Response type | application/json |
| 200 OK | `{"status": "ok"}` |

### 11. Readiness check

| Field | Value |
| --- | --- |
| Endpoint | GET /health/ready |
| Parameters | None |
| Request body | None |
| Response type | application/json |
| 200 OK | `{"status": "ready"}` |
| 503 Service Unavailable | Names each unreachable dependency. `{"status": "unavailable", "failed": ["redis"]}` |

## Admission token contract (for Reservation)

An admitted buyer gets an `admission_token` from `POST /api/v1/events/{event_id}/queue/entries` or `GET /api/v1/events/{event_id}/queue/me`. The buyer's client sends it to the Reservation service when it asks for a hold, in whatever field or header the Reservation API defines. Reservation verifies it **locally** with Queue's public keys, so there is no call to Queue per request. This section is everything Reservation needs to do that.

### Format

A JWT in compact form (`header.claims.signature`), signed with **RS256** (RSASSA-PKCS1-v1_5 with SHA-256) using an RSA key of at least 2048 bits.

**Header:** exactly these three fields.

| Field | Value |
| --- | --- |
| `alg` | `"RS256"`, always. |
| `typ` | `"JWT"` |
| `kid` | ID of the signing key, e.g. `"queue-2026-10"`. Used to pick the key from the JWKS. |

**Claims:** exactly the admission object, nothing else. All values are strings.

| Claim | Format | Meaning |
| --- | --- | --- |
| `admission_id` | UUID | Unique per admission. Store it with the hold and pass it to Checkout. |
| `user_id` | UUID | The admitted buyer. |
| `event_id` | UUID | The event the admission is for. |
| `admitted_at` | ISO 8601 UTC | When the buyer was admitted. |
| `selection_expires_at` | ISO 8601 UTC | `admitted_at` + 5 minutes. **Holds may be created only before this instant.** |
| `admission_expires_at` | ISO 8601 UTC | `admitted_at` + 11 minutes. The admission's end: the last possible hold (6 minutes long) expires here. |

- **Timestamps** always look like `2026-11-20T12:05:00Z`: UTC, a literal `Z`, whole seconds (sub-seconds are dropped, so a deadline is never later than Queue's own).
- **No registered JWT claims:** there is no `exp`, `iat`, `nbf`, `iss`, `aud` or `sub`. Configure the JWT library so it **does not require `exp`**, and enforce the deadline from `selection_expires_at` yourself (step 7 below).

Example (decoded):

```json
{ "alg": "RS256", "typ": "JWT", "kid": "queue-2026-10" }
```

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

### Public keys (JWKS)

Fetch them from `GET /.well-known/jwks.json` (endpoint 9, inside the cluster network):

```json
{"keys": [{"kty": "RSA", "kid": "queue-2026-10", "use": "sig", "alg": "RS256", "n": "0vx7agoebGcQSuu…", "e": "AQAB"}]}
```

- **Cache the key set.** The response allows 5 minutes (`max-age=300`). Look keys up by `kid`.
- **Unknown `kid`:** refetch the JWKS once, then reject if the `kid` is still missing. Rate-limit refetches (e.g. at most one every 30 seconds) so junk tokens can't hammer Queue. Keep using cached keys if Queue is briefly unreachable.
- Use only keys with `"kty": "RSA"`, `"alg": "RS256"` and `"use": "sig"`.
- **Rotation:** Queue starts signing with a new `kid` straight away, so the first token under a new key always has a `kid` Reservation hasn't cached yet. That's why the refetch above is required, not optional. The old key stays in the JWKS for at least 11 minutes, so tokens signed before the switch keep validating until their admission has ended.

### Validation, in order

Reject the request if any step fails.

| # | Check | Reject when |
| --- | --- | --- |
| 1 | Read the header without trusting it. `alg` must be exactly `RS256`. | Any other `alg`, including `none` and `HS256`. |
| 2 | Find the key by `kid` (refetch once, as above). | The `kid` is still unknown. |
| 3 | Verify the signature with that key, **pinning the algorithm to RS256** in the library call (never take it from the token). | The signature is invalid. |
| 4 | All six claims are present. | A claim is missing. |
| 5 | `user_id` equals the authenticated buyer making this request. | It belongs to someone else. |
| 6 | `event_id` equals the event in this request. | It's for another event. |
| 7 | Reservation's current UTC time is **before** `selection_expires_at`. Use a synced server clock (NTP) and **no grace period**. | `now >= selection_expires_at`. |
| 8 | The event itself still allows holds (Reservation's own event status). | The event is cancelled. A token stays cryptographically valid after cancellation, so this check is Reservation's job. |

After the checks pass:

- **Store `admission_id` with the hold** and pass it to Checkout. Checkout's `OrderConfirmed` message must carry `admission_id`, `user_id` and `event_id` back to Queue. That's how the buyer's slot is freed for the next person.

### Behaviour to rely on

- **Tokens aren't single-use.** Polling returns the identical token for as long as the admission lasts, and the buyer may retry. Key idempotency on `admission_id`, not on the token string: after a key rotation, the same admission arrives as a different token.
- **Queue doesn't track holds.** An admission ends only through `OrderConfirmed` for its `admission_id`, at its 11-minute `admission_expires_at`, or when the event is cancelled. A hold expiring doesn't end it.
- **Optional live check:** `GET /internal/v1/admissions/{admission_id}` (endpoint 8) returns the admission while it is active and 404 `ADMISSION_NOT_FOUND` once it has ended. It isn't needed for validation; the token alone is enough.

### Reference implementation (Python, PyJWT)

```python
from datetime import UTC, datetime

import jwt

CLAIMS = ["admission_id", "user_id", "event_id",
          "admitted_at", "selection_expires_at", "admission_expires_at"]

# Caches the key set and refetches it when a token's kid is unknown.
jwks = jwt.PyJWKClient("http://<queue-service>/.well-known/jwks.json", lifespan=300)


def validate_admission_token(token: str, buyer_id: str, event_id: str) -> dict:
    if jwt.get_unverified_header(token).get("alg") != "RS256":           # 1
        raise PermissionError("algorithm")
    key = jwks.get_signing_key_from_jwt(token)                            # 2
    claims = jwt.decode(token, key.key, algorithms=["RS256"],             # 3 (alg pinned)
                        options={"require": CLAIMS})                      # 4 (no exp required)
    if claims["user_id"] != buyer_id:                                     # 5
        raise PermissionError("user_mismatch")
    if claims["event_id"] != event_id:                                    # 6
        raise PermissionError("event_mismatch")
    if datetime.now(UTC) >= datetime.fromisoformat(claims["selection_expires_at"]):  # 7
        raise PermissionError("selection_expired")
    # 8: check the event is not cancelled, in Reservation's own data.
    return claims  # keep claims["admission_id"] with the hold
```

Queue's test suite validates every issued token with these same steps (`tests/fakes/reservation_service.py`), so the tokens and this contract can't drift apart.
