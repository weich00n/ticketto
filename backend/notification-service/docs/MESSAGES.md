# notification-service message contract

What notification-service consumes from the broker, what it does with each message, and the two HTTP calls it depends on. If you publish one of these messages, this page tells you which fields notification-service needs and how it reacts when they're wrong.

The broker contract itself (envelope, exchanges, publishing rules) is owned by [rabbitmq-service](../../rabbitmq-service): [`topology/messages.yaml`](../../rabbitmq-service/topology/messages.yaml), [`contracts/`](../../rabbitmq-service/contracts) and [`docs/MESSAGING.md`](../../rabbitmq-service/docs/MESSAGING.md). This page only adds what's specific to this consumer. The code that enforces it is [`app/messages.py`](../app/messages.py).

## Summary

| Message | Exchange, routing key | Publisher | Queue | Result |
|---|---|---|---|---|
| `EventPublished` | `event.events`, `event.published` | event-service | `notification-service.EventPublished` | One email to every user with new-event emails on |
| `EventCancelled` | `event.events`, `event.cancelled` | event-service | `notification-service.EventCancelled` | One email to every buyer of the event |
| `OrderRefunded` | `checkout.events`, `order.refunded` | checkout-service | `notification-service.OrderRefunded` | One email to the refunded buyer |
| `RefundFailed` | `checkout.events`, `refund.failed` | checkout-service | `notification-service.RefundFailed` | One email to the buyer: the refund didn't go through |
| `TicketIssued` | `ticket.events`, `ticket.issued` | ticket-service | `notification-service.TicketIssued` | One email to the buyer with a QR code per ticket, and one item in their notifications |

notification-service **publishes nothing**. Its broker user (`notification-service`) can only read these queues; it can't read their DLQs or publish anywhere.

Each queue has a dead-letter queue, `<queue>.dlq`. The broker moves a message there when it's rejected as poison, or after 5 redeliveries.

## Envelope

Every message must be in the standard envelope ([`envelope.schema.json`](../../rabbitmq-service/contracts/envelope.schema.json)). notification-service checks it strictly: a message that breaks any of these rules is poison.

| Field | Rule |
|---|---|
| `message_id` | Lower-case UUID. Used for deduplication |
| `message_type` | PascalCase, e.g. `EventPublished`. Decides how the payload is read |
| `schema_version` | Exactly `1`. Any other version is poison |
| `occurred_at` | UTC with a `Z`, e.g. `2026-10-20T08:00:00Z` |
| `producer` | `<name>-service` |
| `payload` | JSON object |

The body must have exactly these six fields; an extra top-level field is poison. The correlation id travels as the AMQP `correlation_id` property, not in the body, and is copied into this service's logs.

## Payloads

Fields not listed are ignored, so publishers can add fields without breaking this consumer. Times must be UTC with a `Z`. Leading and trailing spaces in text are trimmed.

### `EventPublished`

Full schema: [`EventPublished.payload.schema.json`](../../rabbitmq-service/contracts/EventPublished.payload.schema.json). notification-service reads:

| Field | Required | Rule | Used for |
|---|---|---|---|
| `event_id` | yes | UUID | Which event |
| `title` | yes | 1–160 characters | Email subject and body; saved for later cancellation and refund emails |
| `starts_at` | yes | UTC time | "When" in the email (shown in SGT) |
| `release_at` | no | UTC time | When tickets go on sale |
| `queue_join_opens_at` | no | UTC time | When the queue opens |

As event-service publishes it. Only the five fields in the table are read; `venue`, `event_version`, `published_at`, `total_capacity` and `ticket_categories` are for other consumers and are ignored here.

```json
{
  "message_id": "3f2b8c1e-9a4d-4e6b-8c7a-1d2e3f4a5b6c",
  "message_type": "EventPublished",
  "schema_version": 1,
  "occurred_at": "2026-10-20T08:00:00Z",
  "producer": "event-service",
  "payload": {
    "event_id": "a14ec682-62f5-46db-b94a-a8d95435e274",
    "event_version": 2,
    "title": "Ticketto Live 2026",
    "venue": "Singapore Indoor Stadium",
    "starts_at": "2026-12-05T12:00:00Z",
    "release_at": "2026-11-01T02:00:00Z",
    "queue_join_opens_at": "2026-11-01T01:45:00Z",
    "published_at": "2026-10-20T08:00:00Z",
    "total_capacity": 500,
    "ticket_categories": [
      {
        "category_id": "6b9f1d2c-3a4e-4f5a-8b7c-0d1e2f3a4b5c",
        "name": "CAT 1",
        "price_amount": 8800,
        "currency": "SGD",
        "total_quantity": 500
      }
    ]
  }
}
```

**Who gets an email:** every user user-service lists with `notify_new_events=true`. The setting is on by default.

**Sent once per event.** notification-service records that it announced the event. A second `EventPublished` for the same event, even under a new `message_id`, sends nothing. Event updates should therefore not be published as `EventPublished`.

**Skipped if the event is already cancelled**, even when the `EventCancelled` arrived first.

### `EventCancelled`

Full schema: [`EventCancelled.payload.schema.json`](../../rabbitmq-service/contracts/EventCancelled.payload.schema.json). notification-service reads:

| Field | Required | Rule | Used for |
|---|---|---|---|
| `event_id` | yes | UUID | Which event |
| `reason` | no | Up to 500 characters | Shown to buyers as "Reason: …" |
| `cancelled_at` | no | UTC time | Recorded; defaults to when it was processed |

As event-service publishes it. Only `event_id`, `reason` and `cancelled_at` are read; `event_version` and `cancellation_id` are ignored here.

```json
{
  "message_id": "9c8b7a6d-5e4f-4a3b-9c2d-1e0f9a8b7c6d",
  "message_type": "EventCancelled",
  "schema_version": 1,
  "occurred_at": "2026-10-25T09:30:00Z",
  "producer": "event-service",
  "payload": {
    "event_id": "a14ec682-62f5-46db-b94a-a8d95435e274",
    "event_version": 3,
    "cancellation_id": "2d4f6a8c-1b3e-4d5f-a7c9-8e0b2d4f6a8c",
    "cancelled_at": "2026-10-25T09:30:00Z",
    "reason": "The venue is no longer available."
  }
}
```

**Who gets an email:** every user user-service lists with `purchased_event_id=<event_id>`, whatever their new-event setting.

It also cancels any new-event emails for that event that haven't been sent yet. A buyer gets one cancellation email per event, however many times the message is sent.

### `OrderRefunded`

There's no schema in rabbitmq-service's `contracts/` yet. This is what checkout-service sends ([`order_refunded_data`](../../checkout-service/app/messaging/events.py)) and what notification-service requires:

| Field | Required | Rule | Used for |
|---|---|---|---|
| `order_id` | yes | UUID | Shown in the email |
| `user_id` | yes | 1–64 characters; compared lower-case | The buyer's **Cognito sub**, looked up in user-service |
| `event_id` | yes | UUID | The event's title |
| `refund_id` | no | 1–64 characters | Deduplication; falls back to `order_id` |
| `amount` | yes | Integer ≥ 0, minor units (`8800` = 88.00). A string or decimal is poison | Shown as `SGD 88.00` |
| `currency` | no | 3 letters; default `SGD` | Shown with the amount |
| `reason` | no | Up to 64 characters | Explained in words (below) |

```json
{
  "message_id": "5d3c2b1a-7e8f-4a90-b1c2-d3e4f5a6b7c8",
  "message_type": "OrderRefunded",
  "schema_version": 1,
  "occurred_at": "2026-10-25T10:00:00Z",
  "producer": "checkout-service",
  "payload": {
    "order_id": "8a1f0c2e-4b7d-4e19-9c3a-2f6d5e8b7a10",
    "user_id": "c4e8a2d6-1f3b-4c5d-8e7f-9a0b1c2d3e4f",
    "event_id": "a14ec682-62f5-46db-b94a-a8d95435e274",
    "refund_id": "e7d6c5b4-a392-4817-b6f5-e4d3c2b1a098",
    "amount": 8800,
    "currency": "SGD",
    "reason": "event_cancelled"
  }
}
```

**Who gets an email:** the user whose Cognito sub is `user_id`. One email per refund: the same `refund_id` again sends nothing.

Reason codes the email explains (checkout-service `app/service.py`). Any other code still sends the email, without a reason.

| `reason` | Email says |
|---|---|
| `event_cancelled` | the event was cancelled |
| `refund_required` | your booking could not be completed |
| `confirm_rejected` | we could not confirm your seats |
| `late_payment`, `expired_payment` | your payment arrived after your seats were released |

### `RefundFailed`

checkout-service sends this once, when Stripe rejects a refund, and doesn't retry: a person has to follow it up ([`refund_failed_data`](../../checkout-service/app/messaging/events.py)). A refund that Stripe accepts and fails later sends nothing, so nobody is emailed.

The fields are `OrderRefunded`'s, with two differences:

| Field | Difference |
|---|---|
| `refund_id` | Required: a missing one is poison. The same `refund_id` again sends nothing |
| `last_error` | Ignored: raw Stripe text, never stored or shown to the buyer |

**Who gets an email:** the user whose Cognito sub is `user_id`. It asks them to reply with their order ID, so someone has to read the sender's inbox. The service also logs `refund failed` as an error, with `refund_id` and `order_id`.

### `TicketIssued`

ticket-service sends this once per order, after issuing its tickets. The schema is [`contracts/TicketIssued.payload.schema.json`](../../rabbitmq-service/contracts/TicketIssued.payload.schema.json).

| Field | Used for |
|---|---|
| `order_id` | Shown in the email. Links a later refund to this order |
| `user_id` | The buyer's Cognito sub: whose notifications this appears in, and who to look up for the name on the ticket |
| `event_id` | Which event's cancellation stops this email |
| `delivery_email` | Where the email goes. If null, the address comes from user-service, and a failed lookup is retried |
| `title`, `venue`, `starts_at`, `category_name` | Shown in the email. Each can be null. A null `title` or `starts_at` comes from the `events` row, then event-service; the title then falls back to "your event" |
| `tickets[].ticket_number`, `tickets[].ticket_id` | Shown on each ticket in the email, which looks like the ticket on the site |
| `tickets[].qr_payload` | Generated into the ticket's QR code as an inline image |

Rules:

- **Poison:** no `tickets`, more than 5, a ticket with no `qr_payload`, or a malformed ID or time goes straight to the DLQ.
- **Duplicates:** the same `message_id` sends nothing. A new `message_id` for the same order sends the email again and adds a second notification, because that's how ticket-service's republish endpoint resends tickets.
- **Tickets that are no longer valid:** if the event is cancelled or the order refunded first, nothing is queued. If either happens while the email is waiting to go out, it's cancelled. A `RefundFailed` changes nothing, because those tickets are still valid.
- **New-event opt-outs don't apply.**
- **QR codes:** `qr_payload` works like the ticket itself. It's removed from the row once the email is `SENT`, `FAILED` or `CANCELLED`, it's never logged, and the notifications API never returns it. Anyone with broker access can read it while the message is on a queue or a DLQ, so purge DLQs promptly.

## How each message is settled

notification-service's worker runs repeatedly, with a short wait between runs. Each run takes messages with `basic_get`, settles each one, then sends the emails due.

| Outcome | What it does | What happens next |
|---|---|---|
| Handled, or a duplicate | `basic_ack` after the database commit | Removed from the queue |
| Message type it doesn't use | `basic_ack` | Removed and ignored |
| Poison (invalid envelope or payload) | `basic_reject(requeue=False)` | Moved to `<queue>.dlq` at once |
| Transient failure (user-service or the database unavailable; unknown Cognito sub) | `basic_reject(requeue=True)`, then that queue waits for the next run | Retried about a minute later. After 5 redeliveries, moved to `<queue>.dlq` |

It never uses `basic_nack` or auto-ack, and never declares a queue.

Consequences for publishers:
- **Delivery is at least once.** Duplicates are safe. Messages are deduplicated by `message_id`, and every email by recipient: one new-event email and one cancellation email per user per event, and one email per refund.
- **Order doesn't matter.** `EventCancelled` may be handled before `EventPublished`; the cancellation always wins.
- **Latency is usually under about 10 seconds**, plus SES delivery time.
- **A refund for a user user-service doesn't know yet is retried**, then dead-lettered after about 5 minutes. Replay it from the DLQ once the profile exists.

## What notification-service calls

Both calls are inside the VPC and have no API Gateway route. Each has a 2-second timeout.

### user-service (required)

`USER_SERVICE_URL`, e.g. `http://user-service.ticketto.local:8001` (its internal port, which serves only `/internal/v1`).

| Call | Used for | Response |
|---|---|---|
| `GET /internal/v1/users?notify_new_events=true&limit=500[&after=<user_id>]` | `EventPublished` | `{"items": [{"user_id", "cognito_sub", "email", "name"}], "next_after": "<user_id>" \| null}`. `cognito_sub` is stored with each notification, for the notifications API |
| `GET /internal/v1/users?purchased_event_id=<event_id>&limit=500[&after=<user_id>]` | `EventCancelled` | Same shape |
| `GET /internal/v1/users/by-sub/{sub}` | `OrderRefunded`, `RefundFailed`, `TicketIssued` | The user profile (`user_id`, `email`, `name`, …); `404` if no user has that sub |

Lists are paged: notification-service passes `next_after` as `after` until it is `null`.

How errors are handled:
- A `404` from `by-sub` means "not yet": the message is retried.
- Any other error, a timeout, or a malformed response means "user-service is unavailable": the message is retried and nothing is written.

user-service fills these lists itself: it consumes `OrderConfirmed` to record purchases, and `EventCancelled` so that a late `OrderConfirmed` for a cancelled event isn't recorded. See [its README](../../user-service/README.md#messages-the-worker).

### event-service (optional)

`EVENT_SERVICE_URL`. `GET /api/v1/events/{event_id}` is called only when a cancellation or refund concerns an event this service never received an `EventPublished` for, to get its `title` and `starts_at`. Any failure is ignored, and the email says "your event".

## Changing this contract

| Change | Safe? |
|---|---|
| Add a payload field | Yes: unknown fields are ignored |
| Remove or rename a field marked required above | No: the messages become poison and go to the DLQ |
| Add a field to the envelope | No: the envelope is strict |
| A new `schema_version` | No: only `1` is accepted. Ship a consumer that understands it first |
| A new `reason` code | Yes: the email is sent without the explanation. Add it to `REFUND_REASONS` in `app/templates.py` |
| Send another message to notification-service | Add it to its consumers in rabbitmq-service's `messages.yaml`, then a handler here |
