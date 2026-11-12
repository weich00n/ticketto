# Integration test plan: user-service + RabbitMQ + notification-service (AWS)

This plan tests the deployed services in AWS. The local suite (`docker compose run --rm tests`) already tests
the logic against a real broker and Mailpit. In AWS the goal is to prove the deployed parts reach each other,
and that the main paths and the failure paths work there.

**Since then (4 Nov):** notification-service moved from a scheduled Lambda to an always-running ECS worker, so the Lambda steps below now use the worker, and where this says "the Lambda" it means the worker. The broker UI is closed to the internet: use the SSM tunnel. Written 2026-10-28. One test user. `you@example.com` below stands for that user's address, which must be verified in SES.

## Automated: `scripts/e2e/aws_e2e.py`

```sh
cd backend/notification-service
.venv/bin/python scripts/e2e/aws_e2e.py --email you@example.com          # prompts for your password (opt-out step)
.venv/bin/python scripts/e2e/aws_e2e.py --email you@example.com --skip-opt-out
```

It runs scenarios 0–12 below with fresh IDs each time (about 5 minutes):
- **Publishing:** straight to our queues, as in the manual steps.
- **Checking:** each message's outcome from the services' `message processed` log lines (`queued_1`,
  `duplicate_message`, `purchase_recorded`, ...), the DLQs, and that SES accepted the emails.
- **What it prints at the end:** which emails should be in your inbox.
- **What stays manual:** scenario 13 (stopping user-service) and looking at the emails themselves.

First run (2026-10-28): every flow check passed, but **SES refused every send** (`AccessDeniedException` on
the recipient's identity). In the sandbox, `ses:SendEmail` is also checked against each recipient's identity,
and the Lambda role only allowed the sender's. Fixed in `deploy/terraform/main.tf` (any identity, but only
`ses:FromAddress` = the sender). Applied 2026-10-28; the re-run passed everything, including delivery.

**If the broker UI keeps dropping connections** (requests hang ~5 s, then fail): that's a network between your
laptop and AWS cutting plain HTTP on port 15672; the broker itself answers instantly. Use the SSM tunnel and
point the script at it (local port 25672, since a local Docker broker usually holds 15672):

```sh
aws ssm start-session --target i-099e873a0ccf998db --document-name AWS-StartPortForwardingSession \
  --parameters portNumber=15672,localPortNumber=25672      # leave running in another terminal
BROKER_UI_URL=http://localhost:25672 .venv/bin/python scripts/e2e/aws_e2e.py --email you@example.com
```

## The flow under test

```
(you, as checkout) OrderConfirmed ─► user-service.OrderConfirmed ─► user-service worker ─► purchased_event_ids
(you, as event)    EventPublished ─► notification-service.EventPublished ─► worker ─► users?notify_new_events ─► SES
(you, as event)    EventCancelled ─┬► user-service.EventCancelled ─► worker (cancelled_events)
                                   └► notification-service.EventCancelled ─► worker ─► users?purchased_event_id ─► SES
(you, as checkout) OrderRefunded  ─► notification-service.OrderRefunded ─► worker ─► users/by-sub ─► SES
```

## What's live (checked 2026-10-28)

| Thing | State |
|---|---|
| Broker UI | `http://100.54.40.143:15672`, **open to the whole internet** (`management_allowed_cidrs = ["0.0.0.0/0"]`; also an older SG rule "RabbitMQ dashboard" on `sg-093fa59e8d38e8b55`). Log in as `ticketto`, password from `aws secretsmanager get-secret-value --secret-id rabibi/rabbitmq --query SecretString --output text \| jq -r .password`. The IP changes if the instance restarts |
| ECS `notification-service-worker` | 1 running, healthy task |
| SES | Sandbox. Verified: `you@example.com` (recipient), `ticketto.alerts@gmail.com` (sender) |
| user-service worker | Consuming `user-service.OrderConfirmed` and `user-service.EventCancelled` |
| Teammates' consumers | **checkout, queue, reservation and ticket services are live** on the same broker |
| Your Cognito sub | `d4a81448-c0e1-707c-deb8-143fd187c6b1` (this is the `user_id` in OrderConfirmed / OrderRefunded) |

The broker UI is plain HTTP, and the admin password crosses the internet unencrypted. After testing, set
`management_allowed_cidrs = ["<your-ip>/32"]`, or empty to use the SSM tunnel, and `tofu apply`.

## How to publish: to the queue, not the exchange

If you publish to `event.events` or `checkout.events`, the message also reaches the teammates' live services. A
fake `EventPublished` would create inventory in reservation-service and ticket-service, and a fake
`OrderConfirmed` would issue tickets. So publish **straight to our queues**:

1. Broker UI → **Queues and Streams** → click the queue (e.g. `notification-service.EventPublished`).
2. **Publish message**:
   - Delivery mode: **2 - Persistent**
   - Properties: `content_type` = `application/json`, `message_id` = the body's `message_id`, `type` = the
     body's `message_type`
   - Payload: the JSON body
3. `EventCancelled` goes to **both** `user-service.EventCancelled` and `notification-service.EventCancelled`.
   That's what the exchange would do.

Doing this from the UI publishes through the default exchange, so only that one queue gets the message.

## How to check results

There's no database access from the laptop (no ECS Exec, internal API has no gateway route). So check these:

| Where | Command / place | What to look for |
|---|---|---|
| Inbox | Gmail, **including Spam** | The email, from `Ticketto <ticketto.alerts@gmail.com>` |
| Worker logs | `aws logs tail /ecs/notification-service --follow --format short` | `run finished` with counts per queue and `sent` |
| user-service worker | `aws logs tail /ecs/user-service --follow --format short` | The purchase / cancellation being recorded |
| Broker UI | Queues page | Our queues back at 0; `.dlq` counts. In a DLQ, **Get messages** shows the `x-death` header (why) |

---

## Test events

Each scenario uses its own event, because emails are deduplicated per user per event.

| Name | `event_id` | Used in |
|---|---|---|
| A: The Midnight Symphony (you buy it) | `c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90` | 1–6 |
| B: Not bought | `5de3f425-7d24-47ba-ab53-e219cbee1829` | 7, 12 |
| C: Opted out | `e917eb22-d45e-43db-9a53-375dbd0fd7f8` | 8 |
| D: Cancelled before published | `7d374915-aebd-43bb-b184-40d26d904c51` | 9 |

To re-run a scenario later, change the `event_id` **and** every `message_id` (`uuidgen | tr A-Z a-z`).
Otherwise everything is treated as a duplicate and no email is sent.

## Stage 0: preflight

| # | Check | Pass |
|---|---|---|
| 0a | `GET /api/v1/users/me` through API Gateway (or log in on the frontend) | Your profile exists, `notify_new_events: true` |
| 0b | Broker UI: `user-service.OrderConfirmed` and `.EventCancelled` | 1 consumer each |
| 0c | Worker with empty queues | Task healthy; logs show no errors reaching broker or RDS |
| 0d | All `.dlq` counts | Note them down (all 0 on 2026-10-28) so you can spot new ones |

If 0c shows connection errors, it's a security group: the worker's SG must be in rabbitmq-service's `amqp_allowed_sg_ids`,
and needs rules into user-service (8000) and RDS (3306).

## Stage 1: happy path (event A)

### 1. You buy event A → `user-service.OrderConfirmed`

Expect: the worker log records the purchase. Nothing in `user-service.OrderConfirmed.dlq`. (There's no email; the
cancellation email in step 4 proves the purchase was recorded.)

```json
{
  "message_id": "ea7ffac8-ebfc-479f-ab2c-78ba290820d1",
  "message_type": "OrderConfirmed",
  "schema_version": 1,
  "occurred_at": "2026-10-25T10:00:00Z",
  "producer": "checkout-service",
  "payload": {
    "order_id": "49ca2111-197d-4f0e-a548-aeb8d87fd81f",
    "hold_id": "44979012-aaff-4812-aff8-83c5817da4b6",
    "admission_id": "2fec0894-6b7c-4fb4-8550-73560b837323",
    "user_id": "d4a81448-c0e1-707c-deb8-143fd187c6b1",
    "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
    "category_id": "76424420-6e8d-45ac-8e5a-209ee96278c2",
    "quantity": 2,
    "delivery_email": "you@example.com",
    "total_amount": 10000,
    "currency": "SGD",
    "confirmed_at": "2026-10-25T10:00:00Z"
  }
}
```

### 2. Event A published → `notification-service.EventPublished`

Expect: **one new-event email** for "The Midnight Symphony" (you're opted in).

```json
{
  "message_id": "24e5374b-c241-457d-a645-ecb415f7d7bf",
  "message_type": "EventPublished",
  "schema_version": 1,
  "occurred_at": "2026-10-14T08:00:00Z",
  "producer": "event-service",
  "payload": {
    "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
    "event_version": 2,
    "title": "The Midnight Symphony",
    "venue": "Victoria Concert Hall",
    "starts_at": "2026-11-21T19:30:00Z",
    "release_at": "2026-11-20T12:00:00Z",
    "queue_join_opens_at": "2026-11-20T11:45:00Z",
    "published_at": "2026-10-14T08:00:00Z",
    "total_capacity": 300,
    "ticket_categories": [
      { "category_id": "76424420-6e8d-45ac-8e5a-209ee96278c2", "name": "CAT 1", "price_amount": 8800, "currency": "SGD", "total_quantity": 300 }
    ]
  }
}
```

### 3. Duplicates (no new email)

- **3a.** Publish step 2's body again, unchanged. Expect: no email. Logs show it as a duplicate `message_id`.
- **3b.** Publish it again with `message_id` = `17253679-33de-4750-adcf-7a5fc6839c50`. Expect: no email. The same
  event is announced only once.

### 4. Event A cancelled → `user-service.EventCancelled` **and** `notification-service.EventCancelled`

Expect: **one cancellation email** with "Reason: Venue closure". You get it because step 1 made you a buyer.

```json
{
  "message_id": "6b9a5ecb-f219-40cb-92f4-83e44dcb81c6",
  "message_type": "EventCancelled",
  "schema_version": 1,
  "occurred_at": "2026-11-19T09:30:00Z",
  "producer": "event-service",
  "payload": {
    "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
    "event_version": 3,
    "cancellation_id": "0fd4a84a-f4af-4a25-8ead-00430618aaaa",
    "cancelled_at": "2026-11-19T09:30:00Z",
    "reason": "Venue closure"
  }
}
```

### 5. Event A cancelled again, as a new message → `notification-service.EventCancelled`

Expect: **no second email** (one cancellation email per user per event, whatever the `message_id`).

```json
{
  "message_id": "66533031-1f0c-4564-be6b-5778a62b33bc",
  "message_type": "EventCancelled",
  "schema_version": 1,
  "occurred_at": "2026-11-19T09:35:00Z",
  "producer": "event-service",
  "payload": {
    "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
    "event_version": 3,
    "cancellation_id": "0fd4a84a-f4af-4a25-8ead-00430618aaaa",
    "cancelled_at": "2026-11-19T09:30:00Z",
    "reason": "Venue closure"
  }
}
```

### 6. You're refunded for A → `notification-service.OrderRefunded`

Expect: **one refund email**: "SGD 100.00", and that the event was cancelled.

```json
{
  "message_id": "2af2f4ae-1b5a-4e80-af85-e0707981be84",
  "message_type": "OrderRefunded",
  "schema_version": 1,
  "occurred_at": "2026-11-19T10:00:00Z",
  "producer": "checkout-service",
  "payload": {
    "order_id": "49ca2111-197d-4f0e-a548-aeb8d87fd81f",
    "user_id": "d4a81448-c0e1-707c-deb8-143fd187c6b1",
    "event_id": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90",
    "refund_id": "5e6e403e-2b6e-4a8d-922a-127e0521e8f5",
    "amount": 10000,
    "currency": "SGD",
    "reason": "event_cancelled"
  }
}
```

**After stage 1 you should have exactly 3 emails:** new event, cancellation, refund.

## Stage 2: who gets emailed (one user, so you swap roles)

### 6b. Your refund fails → `notification-service.RefundFailed`

Expect: **one email**, "We couldn't complete your refund", saying the money hasn't arrived and asking you to reply. Publish step 6's body with `"message_type": "RefundFailed"`, a new `message_id`, and `"last_error": "StripeRejectedError: test"` in the payload. The error text must not appear in the email.

### 6c. Your tickets are issued → `notification-service.TicketIssued`

Expect: **one email**, "Your 2 tickets for ...", with a QR code per ticket showing in Gmail, and one item in your notifications. The script's QR codes are made up, so they can't be scanned in. To test a real one, buy a ticket on the site and scan its code at check-in.

### 7. Not a buyer: event B

1. `notification-service.EventPublished`. Expect: new-event email (you're opted in).
2. `user-service.EventCancelled` + `notification-service.EventCancelled`. Expect: **no** cancellation email
   (you never bought B).

```json
{
  "message_id": "9daf26b9-d853-4ddc-b0e4-9f30789bb0b7",
  "message_type": "EventPublished",
  "schema_version": 1,
  "occurred_at": "2026-10-28T08:00:00Z",
  "producer": "event-service",
  "payload": {
    "event_id": "5de3f425-7d24-47ba-ab53-e219cbee1829",
    "event_version": 1,
    "title": "Integration Test B (not bought)",
    "venue": "Esplanade",
    "starts_at": "2026-12-12T12:00:00Z",
    "release_at": "2026-11-30T02:00:00Z",
    "queue_join_opens_at": "2026-11-30T01:45:00Z",
    "published_at": "2026-10-28T08:00:00Z",
    "total_capacity": 100,
    "ticket_categories": [
      { "category_id": "1deaabdf-0898-4f19-8d5f-f322f76243b3", "name": "CAT 1", "price_amount": 5000, "currency": "SGD", "total_quantity": 100 }
    ]
  }
}
```

```json
{
  "message_id": "a54f41ff-87cc-4c00-90b3-de46b65878a7",
  "message_type": "EventCancelled",
  "schema_version": 1,
  "occurred_at": "2026-10-28T09:00:00Z",
  "producer": "event-service",
  "payload": {
    "event_id": "5de3f425-7d24-47ba-ab53-e219cbee1829",
    "event_version": 2,
    "cancellation_id": "92ac78a4-bd6a-4218-9533-945c0f29acfe",
    "cancelled_at": "2026-10-28T09:00:00Z",
    "reason": "Integration test"
  }
}
```

### 8. Opted out: event C

1. Turn new-event emails off: `PATCH /api/v1/users/me` with `{"notify_new_events": false}` (frontend, or curl
   with your access token through API Gateway).
2. `notification-service.EventPublished` (below). Expect: **no** email, and the run reports it sent 0.
3. Turn them back on (`true`) before continuing.

```json
{
  "message_id": "91ca8223-f504-468a-9946-b3ebce6131a8",
  "message_type": "EventPublished",
  "schema_version": 1,
  "occurred_at": "2026-10-28T08:00:00Z",
  "producer": "event-service",
  "payload": {
    "event_id": "e917eb22-d45e-43db-9a53-375dbd0fd7f8",
    "event_version": 1,
    "title": "Integration Test C (opted out)",
    "venue": "Esplanade",
    "starts_at": "2026-12-13T12:00:00Z",
    "release_at": "2026-11-30T02:00:00Z",
    "queue_join_opens_at": "2026-11-30T01:45:00Z",
    "published_at": "2026-10-28T08:00:00Z",
    "total_capacity": 100,
    "ticket_categories": [
      { "category_id": "1deaabdf-0898-4f19-8d5f-f322f76243b3", "name": "CAT 1", "price_amount": 5000, "currency": "SGD", "total_quantity": 100 }
    ]
  }
}
```

### 9. Cancellation arrives first: event D

The cancellation must win. To make sure both are handled in the **same** run:

1. Pause the worker: set `worker_desired_count = 0`, then `tofu apply`.
2. Publish the `EventCancelled` to `notification-service.EventCancelled`, then the `EventPublished` to
   `notification-service.EventPublished`.
3. Set `worker_desired_count = 1`, then `tofu apply`.
4. Expect: **no email at all** (not a new-event email, and no cancellation since you didn't buy D).
5. Nothing to re-enable: the worker is back on from step 3.

```json
{
  "message_id": "0a5064b6-65e0-447f-9136-b08ad757a0a5",
  "message_type": "EventCancelled",
  "schema_version": 1,
  "occurred_at": "2026-10-28T09:00:00Z",
  "producer": "event-service",
  "payload": {
    "event_id": "7d374915-aebd-43bb-b184-40d26d904c51",
    "event_version": 2,
    "cancellation_id": "c0f6abb5-b234-4723-9f0c-43e234c005e5",
    "cancelled_at": "2026-10-28T09:00:00Z",
    "reason": "Integration test"
  }
}
```

```json
{
  "message_id": "11f863da-14c0-4816-87bb-e70ed1edd4a0",
  "message_type": "EventPublished",
  "schema_version": 1,
  "occurred_at": "2026-10-28T08:00:00Z",
  "producer": "event-service",
  "payload": {
    "event_id": "7d374915-aebd-43bb-b184-40d26d904c51",
    "event_version": 1,
    "title": "Integration Test D (cancelled first)",
    "venue": "Esplanade",
    "starts_at": "2026-12-14T12:00:00Z",
    "release_at": "2026-11-30T02:00:00Z",
    "queue_join_opens_at": "2026-11-30T01:45:00Z",
    "published_at": "2026-10-28T08:00:00Z",
    "total_capacity": 100,
    "ticket_categories": [
      { "category_id": "1deaabdf-0898-4f19-8d5f-f322f76243b3", "name": "CAT 1", "price_amount": 5000, "currency": "SGD", "total_quantity": 100 }
    ]
  }
}
```

## Stage 3: failures

### 10. Poison messages go straight to the DLQ

Expect: each lands in its `.dlq` at once (`x-death` reason `rejected`), and no email.

- **10a** `notification-service.EventPublished`: step 2's body with `"schema_version": 2` and `message_id`
  `2d9b3a14-f51e-4244-a21c-293118d154f2`.
- **10b** `notification-service.OrderRefunded`: step 6's body with `"amount": "100"` (a string) and `message_id`
  `6833e421-f38d-42a9-b448-a65d2e0c2d6d`.
- **10c** `user-service.OrderConfirmed`: your original OrderConfirmed with `"event_id": "evt-1"` and a fresh `message_id`.

### 11. Unknown buyer is retried, then dead-lettered

`notification-service.OrderRefunded`: step 6's body with a fresh `message_id`, a fresh `refund_id`, and
`"user_id": "00000000-0000-0000-0000-000000000000"`.

Expect: the logs show it rejected and requeued each minute. After about 5 minutes (5 deliveries) it's in
`notification-service.OrderRefunded.dlq`.

### 12. Late purchase for a cancelled event isn't recorded

1. `user-service.OrderConfirmed`: step 1's body with a fresh `message_id`, and `event_id` = event B
   (`5de3f425-7d24-47ba-ab53-e219cbee1829`, cancelled in step 7).
2. Then `notification-service.EventCancelled`: step 7's cancellation with a fresh `message_id`.
3. Expect: **no** cancellation email. If user-service had wrongly recorded the purchase, you'd get one.

### 13. user-service down, then back (optional: stops the API for the team too)

1. Tell the team. `./deploy/service.sh stop` in user-service.
2. `notification-service.OrderRefunded`: step 6's body with a fresh `message_id` and a fresh `refund_id`.
3. Watch 1–2 runs: logs show user-service unavailable, message requeued, no email.
4. `./deploy/service.sh start` **within about 4 minutes**. Expect: exactly one refund email after it's up.
5. Variant: wait more than 5 minutes → it's in the DLQ. Start user-service, then in the broker UI open the
   `.dlq` → **Move messages** back to `notification-service.OrderRefunded`. Expect: one email.

### Not testable with one user

**Email retries ending in `FAILED`.** That needs a profile whose email SES rejects (unverified in the
sandbox), so a second Cognito user and profile. The unit tests cover it.

## Stage 4 (optional): full fan-out through the exchanges

Repeat stage 1 publishing to the **exchanges** (`checkout.events` / `order.confirmed`, `event.events` /
`event.published` and `event.cancelled`, `checkout.events` / `order.refunded`). This checks the bindings and
the services' broker permissions too. Use a new event and new message IDs, and **tell the checkout, queue,
reservation and ticket teams first**: their services will act on it. Every field must be valid against the
contracts (`rabbitmq-service/contracts/`), or the message lands in their DLQs.

## Results

| # | Scenario | Expected | Result | Notes |
|---|---|---|---|---|
| 0 | Preflight | All pass | | |
| 1 | OrderConfirmed A | Recorded, no DLQ | | |
| 2 | EventPublished A | 1 new-event email | | |
| 3 | Duplicates | No email | | |
| 4 | EventCancelled A | 1 cancellation email | | |
| 5 | Cancel A again | No email | | |
| 6 | OrderRefunded A | 1 refund email | | |
| 6b | RefundFailed A | 1 failed-refund email, no error text | | |
| 6c | TicketIssued | 1 ticket email with QR codes; the same message_id again sends nothing | | |
| 7 | Not a buyer (B) | New-event only | | |
| 8 | Opted out (C) | No email | | |
| 9 | Cancel first (D) | No email | | |
| 10 | Poison | DLQ at once | | |
| 11 | Unknown sub | DLQ after ~5 min | | |
| 12 | Late purchase | No email | | |
| 13 | user-service down | 1 email after restart | | |

## Clean up afterwards

- Purge test messages left in DLQs (broker UI → queue → **Purge**). Only purge the `notification-service.*`
  and `user-service.*` ones.
- Lock the broker UI down again (see "What's live").
- Re-enable the schedule if you paused it.
