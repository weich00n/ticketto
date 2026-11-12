# Contract changelog

Changes to the message contracts: `topology/messages.yaml`, `contracts/*.schema.json` and
`contracts/requirements/`. The version is in [`VERSION`](VERSION) and follows the bump rules in
[docs/CONTRACT-TESTING-PLAN.md](../docs/CONTRACT-TESTING-PLAN.md#versioning):

- **patch:** wording, descriptions, example values
- **minor:** a new message, consumer or optional field, or a consumer reading fewer fields
- **major:** anything that can break a publisher or consumer, e.g. a removed or renamed field that
  a consumer reads, a changed routing key, or any envelope change

Every bump adds an entry at the top, naming the messages and services affected.

## [2.0.0] - 2026-11-03

### Removed
- `PaymentCompleted` (checkout-service, `payment.completed`). It had no consumers, and
  checkout-service stopped publishing it. checkout-service's broker user can no longer publish
  to `payment.completed`. Affects checkout-service only.
- Python package: `CONTRACT_KNOWN_GAPS` and `test_known_gaps_name_real_tests`. A test can no longer
  be marked as an expected failure: a service that doesn't match the contracts fails. A test
  module that still sets `CONTRACT_KNOWN_GAPS` is refused with a usage error, so remove it when
  moving to 2.0.0.

## [1.1.0] - 2026-11-03

### Added
- `TicketIssued`: a payload schema and an example, and notification-service as its consumer, with
  its requirements. ticket-service will publish it once per order, and notification-service emails
  the buyer each ticket's details and QR code. `qr_payload` works like the ticket itself, so it
  must never be logged. These are the first contract fields that can be null: `delivery_email`,
  `title`, `venue`, `starts_at` and `category_name` are always sent, but null when ticket-service
  doesn't know the value. Affects ticket-service and notification-service.

## [1.0.0] - 2026-11-03

The first versioned contract. Signed off by the publishing teams.

### Added
- Payload schemas and examples for every message that has consumers:
  - checkout-service: `OrderConfirmed`, `OrderRefunded`, `RefundCompleted`, `RefundFailed`
  - reservation-service: `ReservationExpired`, `RefundRequired`, `EventSoldOut`
  - event-service: `EventPublished` and `EventCancelled` (these two already existed)
- Consumer requirements in `contracts/requirements/`, listing the payload fields each consumer
  reads: checkout-service, notification-service, queue-service, reservation-service,
  ticket-service, user-service. Each listed field must be required in the publisher's schema.

### Changed
- `EventPublished`: `venue` is now a required field. Affects event-service, which already sends
  it; ticket-service reads it.

### Conventions
- Every ID field is a lower-case UUID.
- Every field in a publisher's payload is required; there are no optional fields yet.
- `reason` fields are free text, not enums.
- The envelope's `schema_version` stays at 1. Breaking changes use expand-and-contract or a new
  message name instead.
