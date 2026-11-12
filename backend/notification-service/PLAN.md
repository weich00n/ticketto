# Plan: notifications endpoint for signed-in users

**Goal:** the frontend calls `GET /api/v1/notifications` through the API Gateway and gets the notifications sent to the signed-in user, newest first.

The data already exists: every email notification-service queues is a row in `db_notification.notifications`. What's missing is an HTTP API that reads it.

**Since then (4 Nov):** the API moved from a Lambda to the ECS service `notification-service`, behind the shared VPC link and internal ALB. Identity now comes from the `X-User-Sub` header the gateway overwrites from the verified token. This plan's Lambda design below is kept as the history of the decision; see the README for today's setup.

**Status (31 Oct):** built and tested locally, not deployed. The three decisions at the [end](#decisions-to-confirm) went with the recommendations.

| Step | State |
|---|---|
| user-service: `cognito_sub` in the internal lists | Done on branch `feature/internal-cognito-sub` (not committed) |
| notification-service: migration `0002`, handlers, API, OpenTofu, CI, docs | Done on branch `feature/notifications-api` (not committed). 131 unit + 7 integration tests pass; e2e `inbox` step added |
| Deploy | To do: README → "Adding the notifications API to a running deployment" |
| Route throttling | To do, by the gateway's owner (below) |
| Frontend | To do, with Winnie |

## How it fits in

```
Browser → Next.js /api/proxy (adds the user's access token)
        → API Gateway: Cognito JWT check (the shared authorizer 302-proj-cognito, id 9ky1ji)
        → Lambda notification-service-api (new; same image; inside the VPC)
        → db_notification.notifications, filtered by the token's sub
```

| Choice | Why |
|---|---|
| A **third Lambda** from the same image (handler `app.lambda_handler.api`) | It needs only the database (no broker, no SES). The every-minute Lambda has a different trigger, timeout and job |
| A **Lambda integration** (`AWS_PROXY`, payload 2.0), not a VPC link | API Gateway invokes the Lambda directly, so it doesn't need Cloud Map or a load balancer |
| The user's identity comes from `requestContext.authorizer.jwt.claims.sub` | The gateway has already verified the token. The Lambda never trusts a header or parameter for this |
| No CORS | The browser never calls the gateway directly: the Next.js proxy (`app/api/proxy/[...path]/route.ts`) does |

## Endpoints

All four routes require a valid JWT (`authorization_type = "JWT"`, `authorizer_id = 9ky1ji`).

| Route | Returns |
|---|---|
| `GET /api/v1/notifications?page=1&page_size=20` | `{items, page, page_size, total_items}`: the shape the frontend's `Page<T>` (`lib/api/client.ts`) already uses |
| `GET /api/v1/notifications/unread-count` | `{"unread": 3}`, for a bell badge |
| `POST /api/v1/notifications/{notification_id}/read` | `204`, or `404` if it isn't the caller's |
| `POST /api/v1/notifications/read-all` | `204` |

Each item:

```json
{
  "notification_id": 10,
  "kind": "ORDER_REFUNDED",
  "title": "Your refund of SGD 88.00 for Notification Test",
  "preview": "We've refunded SGD 88.00 for Notification Test because the event was cancelled.",
  "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
  "order_id": "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b",
  "created_at": "2026-10-28T12:50:56Z",
  "email_status": "SENT",
  "read_at": null
}
```

- `title` and `preview` are rendered from the row's `context` by the existing email templates (`app/templates.py`).
- `kind` is `EVENT_PUBLISHED`, `EVENT_CANCELLED`, `ORDER_REFUNDED` or `REFUND_FAILED`.
- `email_status` is `SENT`, `PENDING` or `FAILED`. Rows that are `CANCELLED` (new-event emails withdrawn because the event was cancelled) are hidden.
- Errors use the team's shape: `{"error": {"code": "...", "message": "..."}}`.
- `page_size` is 1–100, with a default of 20.

### What "read" means

A notification is **read once the user has seen it in the app**, not when they open the email. The frontend says so explicitly:

| User action | Frontend calls | Effect |
|---|---|---|
| Opens the bell dropdown | `POST /api/v1/notifications/read-all` | Every unread notification gets `read_at = now`; the badge goes to 0 |
| Clicks one notification (e.g. on a full notifications page) | `POST /api/v1/notifications/{id}/read` | That one gets `read_at = now` |

- `read_at` is `NULL` until then. `unread-count` counts the caller's visible rows where it's `NULL`.
- Marking is idempotent: the first `read_at` is kept.
- **Email opens are not tracked.** That would need SES open tracking (a tracking pixel, a configuration set, and an event pipeline back to the service), and Gmail's image proxy makes it unreliable. Reading the email doesn't clear the bell; opening the bell does.

### Access

- **The Lambda has no public URL:** no function URL. It can only be invoked by API Gateway, through a resource permission limited to this API and the `/api/v1/notifications*` routes, and by IAM principals in the account.
- **The gateway routes are public but require a valid Cognito token.** Without one the gateway answers `401` and the Lambda isn't invoked.
- **Each user only sees their own rows,** matched on the token's `sub`. Another user's notification ID gets `404`.
- **Throttling** on the four routes: 20 requests/s, burst 40. One client polling hard can't run up Lambda and database load.

## The data gap: user_id vs Cognito sub

Rows are stored with **user-service's `user_id`**, but the token carries the **Cognito `sub`**.

| Change | Where |
|---|---|
| Migration `0002`: add `cognito_sub VARCHAR(64) NULL` and `read_at DATETIME NULL` to `notifications`, plus an index on `(cognito_sub, created_at)` | notification-service |
| The internal user lists (`GET /internal/v1/users?...`) also return `cognito_sub`. This is additive, and notification-service is its only caller | **user-service**: `app/schemas.py`, plus a test |
| New rows store the sub:<br>• new-event and cancellation emails: from user-service's list<br>• refunds: from the message's `user_id`, which is already the sub | notification-service: `app/user_client.py` (`Recipient` gains `cognito_sub`), `app/handlers.py` |

The few existing rows (test data from the e2e runs) have no sub, so they won't show up.

## Infrastructure

In `deploy/terraform`, following checkout-service's `gateway.tf` pattern: each service adds only its own pieces to the shared API.

| Resource | Notes |
|---|---|
| `aws_lambda_function.api` (`notification-service-api`) | Same image, VPC and security group. `image_config.command = ["app.lambda_handler.api"]`. Database variables only. 10 s timeout, 512 MB |
| `aws_cloudwatch_log_group.api` | 14 days' retention, like the others |
| `aws_apigatewayv2_integration.notifications` | `AWS_PROXY`, `payload_format_version = "2.0"` |
| `aws_apigatewayv2_route.notifications` | The four routes above, JWT authorizer `9ky1ji` |
| `aws_lambda_permission.api_gateway` | Lets API Gateway invoke the function. `source_arn` is limited to this API and the notifications routes (`…/*/*/api/v1/notifications*`) |
| Route throttling | 20 requests/s, burst 40, on the four routes. The shared `$default` stage is managed in `frontend/deploy/gateway/logging.tf`, and that stack's next apply would undo settings made anywhere else. So the gateway's owner adds, inside `aws_apigatewayv2_stage.shared`, one `route_settings { route_key = "<route>" throttling_rate_limit = 20 throttling_burst_limit = 40 }` block for each of the four routes |
| No function URL | Deliberately none: API Gateway is the only way in |
| Variables `api_id` (`x0vxfeenrl`), `api_authorizer_id` (`9ky1ji`) | Read, not managed: the shared API, its `$default` stage and the authorizer belong to the team |
| Deploy role | Also allowed to update the API function |

CI:
- `scripts/ci/lambda-deploy.sh` updates the API function too, after the migration.
- New CI variable `API_FUNCTION_NAME` (Protected, scope `production`).

## Tests (written first)

### notification-service, unit

1. Only the caller's rows come back, newest first. Another user's rows never do.
2. Paging: `page`, `page_size`, `total_items`. A bad `page`/`page_size` → `400`.
3. `CANCELLED` rows are hidden. `PENDING` and `FAILED` rows show, with their `email_status`.
4. `title` and `preview` are right for each kind.
5. `unread-count` counts only the caller's unread, visible rows.
6. `POST …/{id}/read`:
   - the caller's own → `204`, sets `read_at`; marking twice is harmless
   - another user's, or one that doesn't exist → `404`
   - a non-integer ID → `400`
7. `read-all` marks only the caller's rows.
8. A request without a `sub` claim → `401`. An unknown route → `404`.
9. The handlers store `cognito_sub` on new rows, for all three kinds.
10. Migration `0002` upgrades and downgrades cleanly.

### user-service, unit

11. The internal list items include `cognito_sub`.

### notification-service, integration (local compose)

12. Publish messages to the local broker, then call the API handler with a fake gateway event: the list shows exactly that user's emails.

### e2e (AWS, `scripts/e2e/aws_e2e.py`)

13. After a run, sign in and call `GET /api/v1/notifications` through the real gateway: it lists this run's notifications.

## Order of work

1. **user-service:** add `cognito_sub` to the internal list, with its test. Merge and deploy first. It's additive, so it's safe on its own.
2. **notification-service:**
   1. Tests 1–10 (failing).
   2. Migration `0002`, handler changes, the API handler. Run the tests until they pass, then lint.
   3. OpenTofu and CI.
   4. Integration test 12 and e2e test 13.
   5. Docs: `README.md` and `docs/MESSAGES.md`.
3. **AWS:**
   1. Merge to `main`: the pipeline migrates the database and deploys the code.
   2. Add `API_FUNCTION_NAME` to GitLab.
   3. `tofu apply`: creates the API function and the gateway routes.
   4. Re-run `deploy` once, so the new function gets the build.
4. **Frontend** (with its owner):
   - The bell already exists: `components/layout/notification-bell.tsx`. It's shown to signed-in users and is a placeholder: its dropdown always says "You're all caught up".
   - Wire it up:
     - **Badge:** `GET /api/v1/notifications/unread-count`, refreshed about once a minute.
     - **Dropdown:** the latest few from `GET /api/v1/notifications?page_size=10`, all through `lib/api/client.ts`.
     - **Opening it** marks them read (`read-all`); clicking one marks just that one (`/{id}/read`).
   - Optionally, a full notifications page for older items, using `page`.
   - It needs `API_GATEWAY_URL` set and `USE_DEMO_DATA=false`.

**Effort:** about 1½–2 days for the backend, plus the frontend page.

## Decisions to confirm

| # | Question | Recommendation |
|---|---|---|
| 1 | How to match the token to rows | **Store `cognito_sub` on each row**: one small user-service change. The alternative, calling user-service's `by-sub` on every request, needs no user-service change. But it adds a call to every page load, and the page fails whenever user-service is down |
| 2 | Show notifications whose email failed (e.g. unverified addresses in the SES sandbox)? | **Yes**: the in-app list works even when the email doesn't |
| 3 | Read/unread and a bell badge, or a plain list first? | Include it: it's only `read_at`, two small endpoints and their tests |
