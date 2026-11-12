# AMQP message contracts

<!-- Generated from topology/messages.yaml and contracts/ by tools/generate_definitions.py. Do not edit. -->

Contract version **2.0.0** ([changelog](../contracts/CHANGELOG.md)). Generated from [`topology/messages.yaml`](../topology/messages.yaml) and [`contracts/`](../contracts/); edit those, not this page. How to publish and consume: [MESSAGING.md](MESSAGING.md).

- **Publishers own their payload schemas** (`contracts/<Message>.payload.schema.json`). Every field listed is sent; consumers must ignore fields they don't know.
- **Consumers list the fields they read** (`contracts/requirements/<consumer>.yaml`). CI fails if a publisher's schema stops requiring any of them.

## Messages

| Message | Publisher | Exchange, routing key | Consumers |
|---|---|---|---|
| [`EventPublished`](#eventpublished) | event | `event.events`, `event.published` | queue, reservation, ticket, notification |
| [`EventCancelled`](#eventcancelled) | event | `event.events`, `event.cancelled` | queue, reservation, checkout, ticket, notification, user |
| [`OrderConfirmed`](#orderconfirmed) | checkout | `checkout.events`, `order.confirmed` | ticket, queue, user |
| [`OrderRefunded`](#orderrefunded) | checkout | `checkout.events`, `order.refunded` | notification |
| [`RefundCompleted`](#refundcompleted) | checkout | `checkout.events`, `refund.completed` | ticket |
| [`RefundFailed`](#refundfailed) | checkout | `checkout.events`, `refund.failed` | notification |
| [`ReservationExpired`](#reservationexpired) | reservation | `reservation.events`, `reservation.expired` | checkout |
| [`RefundRequired`](#refundrequired) | reservation | `reservation.events`, `refund.required` | checkout |
| [`EventSoldOut`](#eventsoldout) | reservation | `reservation.events`, `event.sold_out` | queue |
| [`TicketIssued`](#ticketissued) | ticket | `ticket.events`, `ticket.issued` | notification |

## Envelope

Every message body, whatever its type. The message-specific fields are in `payload`.

| Field | Type | Required | Description |
|---|---|---|---|
| `message_id` | UUID | yes | UUID. Also sent as the AMQP message_id property; consumers deduplicate on it. |
| `message_type` | string | yes | PascalCase message name from topology/messages.yaml. Also the AMQP type property. Not the routing key: that is the message's dotted routing_key. |
| `schema_version` | integer ≥ 1 | yes | Version of this message's payload schema. Consumers reject versions they don't know. |
| `occurred_at` | UTC timestamp | yes | UTC, ISO 8601 with a Z suffix. |
| `producer` | string | yes | The publishing service, as named in topology/messages.yaml. |
| `payload` | object | yes |  |

## Payloads

### EventPublished

- **Publisher:** event-service (implemented)
- **Exchange, routing key:** `event.events`, `event.published`
- **Consumer queues:** `queue-service.EventPublished`, `reservation-service.EventPublished`, `ticket-service.EventPublished`, `notification-service.EventPublished`

Published by event-service when an organiser publishes an event (event-service app/outbox.py). Consumers must ignore fields they don't know. Which consumers read which fields: contracts/requirements/.

| Field | Type | Required | Description | Read by |
|---|---|---|---|---|
| `event_id` | UUID | yes |  | notification, queue, reservation, ticket |
| `event_version` | integer ≥ 1 | yes | Increases on every change to the event. A consumer ignores a message older than the version it already has. | queue, reservation, ticket |
| `title` | string | yes |  | notification, reservation, ticket |
| `venue` | string | yes |  | ticket |
| `starts_at` | UTC timestamp | yes |  | notification, queue, reservation, ticket |
| `release_at` | UTC timestamp | yes | When tickets go on sale. | queue, reservation |
| `queue_join_opens_at` | UTC timestamp | yes | Always release_at minus 15 minutes. | queue, reservation |
| `published_at` | UTC timestamp | yes |  | reservation |
| `total_capacity` | integer ≥ 0 | yes | Sum of total_quantity over ticket_categories. | queue, reservation |
| `ticket_categories` | array of object | yes |  | reservation, ticket |
| `ticket_categories[].category_id` | UUID | yes |  | reservation, ticket |
| `ticket_categories[].name` | string | yes |  | reservation, ticket |
| `ticket_categories[].price_amount` | integer ≥ 0 | yes | Integer minor units: 8800 = SGD 88.00. | reservation |
| `ticket_categories[].currency` | `"SGD"` | yes |  | reservation |
| `ticket_categories[].total_quantity` | integer ≥ 1 | yes |  | reservation |

Example:

```json
{
  "message_id": "3d6f0c8e-6a3b-4f7d-9a51-0b2c1e7d9f40",
  "message_type": "EventPublished",
  "schema_version": 1,
  "occurred_at": "2026-10-06T10:05:00Z",
  "producer": "event-service",
  "payload": {
    "event_id": "a14ec682-62f5-46db-b94a-a8d95435e274",
    "event_version": 2,
    "title": "Autumn Live",
    "venue": "Victoria Concert Hall",
    "starts_at": "2026-11-20T12:00:00Z",
    "release_at": "2026-10-20T12:00:00Z",
    "queue_join_opens_at": "2026-10-20T11:45:00Z",
    "published_at": "2026-10-06T10:05:00Z",
    "total_capacity": 500,
    "ticket_categories": [
      {
        "category_id": "b05e8625-6d43-4252-ad2c-bda3222a769b",
        "name": "CAT 1",
        "price_amount": 8800,
        "currency": "SGD",
        "total_quantity": 500
      }
    ]
  }
}
```

### EventCancelled

- **Publisher:** event-service (implemented)
- **Exchange, routing key:** `event.events`, `event.cancelled`
- **Consumer queues:** `queue-service.EventCancelled`, `reservation-service.EventCancelled`, `checkout-service.EventCancelled`, `ticket-service.EventCancelled`, `notification-service.EventCancelled`, `user-service.EventCancelled`

Published by event-service when an organiser cancels a published event (event-service app/outbox.py). A cancellation takes precedence over any earlier EventPublished for the same event, even if they arrive out of order. Consumers must ignore fields they don't know. Which consumers read which fields: contracts/requirements/.

| Field | Type | Required | Description | Read by |
|---|---|---|---|---|
| `event_id` | UUID | yes |  | checkout, notification, queue, reservation, ticket, user |
| `event_version` | integer ≥ 1 | yes | The event's version after the cancellation. | checkout, queue, reservation, ticket |
| `cancellation_id` | UUID | yes |  | checkout, reservation, ticket |
| `cancelled_at` | UTC timestamp | yes |  | checkout, queue, reservation, ticket |
| `reason` | string | yes |  | checkout, reservation, ticket |

Example:

```json
{
  "message_id": "9b2e4f71-1c0a-4d3e-8f6b-5a7c2d9e0f13",
  "message_type": "EventCancelled",
  "schema_version": 1,
  "occurred_at": "2026-10-06T11:00:00Z",
  "producer": "event-service",
  "payload": {
    "event_id": "a14ec682-62f5-46db-b94a-a8d95435e274",
    "event_version": 3,
    "cancellation_id": "c41a7e2d-5f86-4b09-9e3c-7d2b1a0f6e58",
    "cancelled_at": "2026-10-06T11:00:00Z",
    "reason": "Event cancelled by organiser."
  }
}
```

### OrderConfirmed

- **Publisher:** checkout-service (implemented)
- **Exchange, routing key:** `checkout.events`, `order.confirmed`
- **Consumer queues:** `ticket-service.OrderConfirmed`, `queue-service.OrderConfirmed`, `user-service.OrderConfirmed`

Published by checkout-service once an order is paid for. Every field is always sent. Consumers must ignore fields they don't know. Which consumers read which fields: contracts/requirements/.

| Field | Type | Required | Description | Read by |
|---|---|---|---|---|
| `order_id` | UUID | yes |  | queue, ticket |
| `hold_id` | UUID | yes |  | ticket |
| `admission_id` | UUID | yes | The queue admission the order was made under. | queue, ticket |
| `user_id` | UUID | yes |  | queue, ticket, user |
| `event_id` | UUID | yes |  | queue, ticket, user |
| `category_id` | UUID | yes |  | ticket |
| `quantity` | integer ≥ 1 | yes |  | ticket |
| `delivery_email` | email | yes | Where the tickets are sent. | ticket |
| `total_amount` | integer ≥ 0 | yes | Integer minor units: 10000 = SGD 100.00. | ticket |
| `currency` | `"SGD"` | yes |  | ticket |
| `confirmed_at` | UTC timestamp | yes |  | queue, ticket |

Example:

```json
{
  "message_id": "5d3c2b1a-7e8f-4a90-b1c2-d3e4f5a6b7c8",
  "message_type": "OrderConfirmed",
  "schema_version": 1,
  "occurred_at": "2026-10-20T12:06:15Z",
  "producer": "checkout-service",
  "payload": {
    "order_id": "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b",
    "hold_id": "b1e2d3c4-5f60-4718-92a3-b4c5d6e7f809",
    "admission_id": "8b0e5c1a-4f2d-4c1e-9a77-2d6f0b3e9c41",
    "user_id": "2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33",
    "event_id": "a14ec682-62f5-46db-b94a-a8d95435e274",
    "category_id": "b05e8625-6d43-4252-ad2c-bda3222a769b",
    "quantity": 2,
    "delivery_email": "buyer@example.com",
    "total_amount": 17600,
    "currency": "SGD",
    "confirmed_at": "2026-10-20T12:06:15Z"
  }
}
```

### OrderRefunded

- **Publisher:** checkout-service (implemented)
- **Exchange, routing key:** `checkout.events`, `order.refunded`
- **Consumer queues:** `notification-service.OrderRefunded`

Published by checkout-service once Stripe has refunded an order. Every field is always sent. Consumers must ignore fields they don't know. Which consumers read which fields: contracts/requirements/.

| Field | Type | Required | Description | Read by |
|---|---|---|---|---|
| `order_id` | UUID | yes |  | notification |
| `user_id` | UUID | yes |  | notification |
| `event_id` | UUID | yes |  | notification |
| `refund_id` | UUID | yes |  |  |
| `amount` | integer ≥ 0 | yes | Integer minor units. | notification |
| `currency` | `"SGD"` | yes |  |  |
| `reason` | string | yes | Free text, e.g. event_cancelled. |  |

Example:

```json
{
  "message_id": "7a6b5c4d-3e2f-4a1b-9c8d-7e6f5a4b3c2d",
  "message_type": "OrderRefunded",
  "schema_version": 1,
  "occurred_at": "2026-10-21T10:00:00Z",
  "producer": "checkout-service",
  "payload": {
    "order_id": "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b",
    "user_id": "2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33",
    "event_id": "a14ec682-62f5-46db-b94a-a8d95435e274",
    "refund_id": "0b7c6d5e-4f3a-4b2c-9d1e-8f7a6b5c4d3e",
    "amount": 17600,
    "currency": "SGD",
    "reason": "event_cancelled"
  }
}
```

### RefundCompleted

- **Publisher:** checkout-service (implemented)
- **Exchange, routing key:** `checkout.events`, `refund.completed`
- **Consumer queues:** `ticket-service.RefundCompleted`

Published by checkout-service when a refund has completed. Every field is always sent. Consumers must ignore fields they don't know. Which consumers read which fields: contracts/requirements/.

| Field | Type | Required | Description | Read by |
|---|---|---|---|---|
| `order_id` | UUID | yes |  | ticket |
| `event_id` | UUID | yes |  | ticket |
| `refund_id` | UUID | yes |  | ticket |
| `amount` | integer ≥ 0 | yes | Integer minor units. | ticket |
| `currency` | `"SGD"` | yes |  | ticket |

Example:

```json
{
  "message_id": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
  "message_type": "RefundCompleted",
  "schema_version": 1,
  "occurred_at": "2026-10-21T10:00:00Z",
  "producer": "checkout-service",
  "payload": {
    "order_id": "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b",
    "event_id": "a14ec682-62f5-46db-b94a-a8d95435e274",
    "refund_id": "0b7c6d5e-4f3a-4b2c-9d1e-8f7a6b5c4d3e",
    "amount": 17600,
    "currency": "SGD"
  }
}
```

### RefundFailed

- **Publisher:** checkout-service (implemented)
- **Exchange, routing key:** `checkout.events`, `refund.failed`
- **Consumer queues:** `notification-service.RefundFailed`

Published by checkout-service when a refund can't be completed. Every field is always sent. Consumers must ignore fields they don't know. Which consumers read which fields: contracts/requirements/.

| Field | Type | Required | Description | Read by |
|---|---|---|---|---|
| `order_id` | UUID | yes |  | notification |
| `user_id` | UUID | yes |  | notification |
| `event_id` | UUID | yes |  | notification |
| `refund_id` | UUID | yes |  | notification |
| `amount` | integer ≥ 0 | yes | Integer minor units. | notification |
| `currency` | `"SGD"` | yes |  |  |
| `reason` | string | yes | Free text, e.g. event_cancelled. |  |
| `last_error` | string | yes | The last error from the payment provider. |  |

Example:

```json
{
  "message_id": "4c3b2a19-0f8e-4d7c-9b6a-5f4e3d2c1b0a",
  "message_type": "RefundFailed",
  "schema_version": 1,
  "occurred_at": "2026-10-21T10:00:00Z",
  "producer": "checkout-service",
  "payload": {
    "order_id": "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b",
    "user_id": "2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33",
    "event_id": "a14ec682-62f5-46db-b94a-a8d95435e274",
    "refund_id": "0b7c6d5e-4f3a-4b2c-9d1e-8f7a6b5c4d3e",
    "amount": 17600,
    "currency": "SGD",
    "reason": "event_cancelled",
    "last_error": "StripeRejectedError: charge already refunded"
  }
}
```

### ReservationExpired

- **Publisher:** reservation-service (implemented)
- **Exchange, routing key:** `reservation.events`, `reservation.expired`
- **Consumer queues:** `checkout-service.ReservationExpired`

Published by reservation-service (HoldExpiryWorker) once a hold's TTL has passed. Every field is always sent. Consumers must ignore fields they don't know. Which consumers read which fields: contracts/requirements/.

| Field | Type | Required | Description | Read by |
|---|---|---|---|---|
| `hold_id` | UUID | yes |  | checkout |
| `admission_id` | UUID | yes |  | checkout |
| `user_id` | UUID | yes |  | checkout |
| `event_id` | UUID | yes |  | checkout |
| `category_id` | UUID | yes |  | checkout |
| `quantity` | integer ≥ 1 | yes |  | checkout |
| `expired_at` | UTC timestamp | yes |  | checkout |

Example:

```json
{
  "message_id": "6e5d4c3b-2a19-4f8e-8d7c-6b5a4f3e2d1c",
  "message_type": "ReservationExpired",
  "schema_version": 1,
  "occurred_at": "2026-10-20T12:10:00Z",
  "producer": "reservation-service",
  "payload": {
    "hold_id": "b1e2d3c4-5f60-4718-92a3-b4c5d6e7f809",
    "admission_id": "8b0e5c1a-4f2d-4c1e-9a77-2d6f0b3e9c41",
    "user_id": "2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33",
    "event_id": "a14ec682-62f5-46db-b94a-a8d95435e274",
    "category_id": "b05e8625-6d43-4252-ad2c-bda3222a769b",
    "quantity": 2,
    "expired_at": "2026-10-20T12:10:00Z"
  }
}
```

### RefundRequired

- **Publisher:** reservation-service (implemented)
- **Exchange, routing key:** `reservation.events`, `refund.required`
- **Consumer queues:** `checkout-service.RefundRequired`

Published by reservation-service for a hold confirmed before its event was cancelled. Every field is always sent. Consumers must ignore fields they don't know. Which consumers read which fields: contracts/requirements/.

| Field | Type | Required | Description | Read by |
|---|---|---|---|---|
| `hold_id` | UUID | yes |  | checkout |
| `order_id` | UUID | yes |  | checkout |
| `user_id` | UUID | yes |  | checkout |
| `event_id` | UUID | yes |  | checkout |
| `total_amount` | integer ≥ 0 | yes | Integer minor units. | checkout |
| `currency` | `"SGD"` | yes |  | checkout |
| `reason` | string | yes | Free text, e.g. EVENT_CANCELLED. | checkout |
| `required_at` | UTC timestamp | yes |  | checkout |

Example:

```json
{
  "message_id": "3f2b1a09-8e7d-4c6b-9a58-4f3e2d1c0b9a",
  "message_type": "RefundRequired",
  "schema_version": 1,
  "occurred_at": "2026-10-21T09:30:00Z",
  "producer": "reservation-service",
  "payload": {
    "hold_id": "b1e2d3c4-5f60-4718-92a3-b4c5d6e7f809",
    "order_id": "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b",
    "user_id": "2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33",
    "event_id": "a14ec682-62f5-46db-b94a-a8d95435e274",
    "total_amount": 17600,
    "currency": "SGD",
    "reason": "EVENT_CANCELLED",
    "required_at": "2026-10-21T09:30:00Z"
  }
}
```

### EventSoldOut

- **Publisher:** reservation-service (implemented)
- **Exchange, routing key:** `reservation.events`, `event.sold_out`
- **Consumer queues:** `queue-service.EventSoldOut`

Published by reservation-service when the last hold empties the event's stock. Every field is always sent. Consumers must ignore fields they don't know. Which consumers read which fields: contracts/requirements/.

| Field | Type | Required | Description | Read by |
|---|---|---|---|---|
| `event_id` | UUID | yes |  | queue |
| `sold_out_at` | UTC timestamp | yes |  | queue |

Example:

```json
{
  "message_id": "8d7c6b5a-4f3e-4d2c-8b1a-0f9e8d7c6b5a",
  "message_type": "EventSoldOut",
  "schema_version": 1,
  "occurred_at": "2026-10-20T12:07:42Z",
  "producer": "reservation-service",
  "payload": {
    "event_id": "a14ec682-62f5-46db-b94a-a8d95435e274",
    "sold_out_at": "2026-10-20T12:07:42Z"
  }
}
```

### TicketIssued

- **Publisher:** ticket-service (planned)
- **Exchange, routing key:** `ticket.events`, `ticket.issued`
- **Consumer queues:** `notification-service.TicketIssued`

Published by ticket-service once per order, after it issues the order's tickets. Every field is always sent; those that allow null are null when ticket-service doesn't know the value. qr_payload works like the ticket itself, so it must never be logged. Consumers must ignore fields they don't know. Which consumers read which fields: contracts/requirements/.

| Field | Type | Required | Description | Read by |
|---|---|---|---|---|
| `order_id` | UUID | yes |  | notification |
| `user_id` | UUID | yes |  | notification |
| `event_id` | UUID | yes |  | notification |
| `delivery_email` | email or null | yes | Where the tickets are sent, from OrderConfirmed. Null if it wasn't a valid address. | notification |
| `title` | string or null | yes |  | notification |
| `venue` | string or null | yes |  | notification |
| `starts_at` | UTC timestamp or null | yes |  | notification |
| `category_name` | string or null | yes |  | notification |
| `tickets` | array of object | yes | The order's valid tickets. An order has at most 5. | notification |
| `tickets[].ticket_id` | UUID | yes |  |  |
| `tickets[].ticket_number` | integer ≥ 1 | yes | This ticket's number within the order, starting at 1. | notification |
| `tickets[].qr_payload` | string | yes | The text to encode in the ticket's QR code: TKT1.<key_id>.<ticket_id>.<signature>. | notification |

Example:

```json
{
  "message_id": "7d6c5b4a-3e2f-4a1b-8c9d-0e1f2a3b4c5d",
  "message_type": "TicketIssued",
  "schema_version": 1,
  "occurred_at": "2026-10-21T10:00:00Z",
  "producer": "ticket-service",
  "payload": {
    "order_id": "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b",
    "user_id": "2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33",
    "event_id": "a14ec682-62f5-46db-b94a-a8d95435e274",
    "delivery_email": "buyer@example.com",
    "title": "Autumn Live",
    "venue": "National Stadium",
    "starts_at": "2026-11-20T12:00:00Z",
    "category_name": "CAT 1",
    "tickets": [
      {
        "ticket_id": "6f1e2d3c-4b5a-4978-8695-a4b3c2d1e0f9",
        "ticket_number": 1,
        "qr_payload": "TKT1.k1.6f1e2d3c-4b5a-4978-8695-a4b3c2d1e0f9.example-signature-not-a-real-one-0123456789"
      },
      {
        "ticket_id": "0a9b8c7d-6e5f-4a3b-9c2d-1e0f9a8b7c6d",
        "ticket_number": 2,
        "qr_payload": "TKT1.k1.0a9b8c7d-6e5f-4a3b-9c2d-1e0f9a8b7c6d.example-signature-not-a-real-one-9876543210"
      }
    ]
  }
}
```

## By service

### checkout-service

**Publishes:**

- [`OrderConfirmed`](#orderconfirmed): `checkout.events`, `order.confirmed`
- [`OrderRefunded`](#orderrefunded): `checkout.events`, `order.refunded`
- [`RefundCompleted`](#refundcompleted): `checkout.events`, `refund.completed`
- [`RefundFailed`](#refundfailed): `checkout.events`, `refund.failed`

**Consumes** (each from its queue `checkout-service.<Message>`), and the payload fields it reads:

- [`EventCancelled`](#eventcancelled): `event_id`, `event_version`, `cancellation_id`, `cancelled_at`, `reason`
- [`ReservationExpired`](#reservationexpired): `hold_id`, `admission_id`, `user_id`, `event_id`, `category_id`, `quantity`, `expired_at`
- [`RefundRequired`](#refundrequired): `hold_id`, `order_id`, `user_id`, `event_id`, `total_amount`, `currency`, `reason`, `required_at`

### event-service

**Publishes:**

- [`EventPublished`](#eventpublished): `event.events`, `event.published`
- [`EventCancelled`](#eventcancelled): `event.events`, `event.cancelled`

**Consumes:**

_None_

### notification-service

**Publishes:**

_None_

**Consumes** (each from its queue `notification-service.<Message>`), and the payload fields it reads:

- [`EventPublished`](#eventpublished): `event_id`, `title`, `starts_at`
- [`EventCancelled`](#eventcancelled): `event_id`
- [`OrderRefunded`](#orderrefunded): `order_id`, `user_id`, `event_id`, `amount`
- [`RefundFailed`](#refundfailed): `order_id`, `user_id`, `event_id`, `refund_id`, `amount`
- [`TicketIssued`](#ticketissued): `order_id`, `user_id`, `event_id`, `delivery_email`, `title`, `venue`, `starts_at`, `category_name`, `tickets[].ticket_number`, `tickets[].qr_payload`

### queue-service

**Publishes:**

_None_

**Consumes** (each from its queue `queue-service.<Message>`), and the payload fields it reads:

- [`EventPublished`](#eventpublished): `event_id`, `event_version`, `total_capacity`, `release_at`, `starts_at`, `queue_join_opens_at`
- [`EventCancelled`](#eventcancelled): `event_id`, `event_version`, `cancelled_at`
- [`OrderConfirmed`](#orderconfirmed): `order_id`, `admission_id`, `user_id`, `event_id`, `confirmed_at`
- [`EventSoldOut`](#eventsoldout): `event_id`, `sold_out_at`

### reservation-service

**Publishes:**

- [`ReservationExpired`](#reservationexpired): `reservation.events`, `reservation.expired`
- [`RefundRequired`](#refundrequired): `reservation.events`, `refund.required`
- [`EventSoldOut`](#eventsoldout): `reservation.events`, `event.sold_out`

**Consumes** (each from its queue `reservation-service.<Message>`), and the payload fields it reads:

- [`EventPublished`](#eventpublished): `event_id`, `event_version`, `title`, `starts_at`, `release_at`, `queue_join_opens_at`, `published_at`, `total_capacity`, `ticket_categories[].category_id`, `ticket_categories[].name`, `ticket_categories[].price_amount`, `ticket_categories[].currency`, `ticket_categories[].total_quantity`
- [`EventCancelled`](#eventcancelled): `event_id`, `event_version`, `cancellation_id`, `cancelled_at`, `reason`

### ticket-service

**Publishes:**

- [`TicketIssued`](#ticketissued): `ticket.events`, `ticket.issued`

**Consumes** (each from its queue `ticket-service.<Message>`), and the payload fields it reads:

- [`EventPublished`](#eventpublished): `event_id`, `event_version`, `title`, `venue`, `starts_at`, `ticket_categories[].category_id`, `ticket_categories[].name`
- [`EventCancelled`](#eventcancelled): `event_id`, `event_version`, `cancellation_id`, `cancelled_at`, `reason`
- [`OrderConfirmed`](#orderconfirmed): `order_id`, `hold_id`, `admission_id`, `user_id`, `event_id`, `category_id`, `quantity`, `delivery_email`, `confirmed_at`, `total_amount`, `currency`
- [`RefundCompleted`](#refundcompleted): `order_id`, `event_id`, `refund_id`, `amount`, `currency`

### user-service

**Publishes:**

_None_

**Consumes** (each from its queue `user-service.<Message>`), and the payload fields it reads:

- [`EventCancelled`](#eventcancelled): `event_id`
- [`OrderConfirmed`](#orderconfirmed): `user_id`, `event_id`
