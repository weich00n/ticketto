# Queue Service – MySQL schema

Source of truth: the Alembic migrations in `migrations/versions/` (head: `0004`). The ORM models are in `app/core/db.py`. Update this file whenever a migration is added.

Database: `queue_service` (our own database inside the shared RDS instance; see Phase 10.4 of the implementation plan). MySQL 8.

| Table | Purpose | Written by | Rows deleted? |
| --- | --- | --- | --- |
| `queue_configs` | One row per event: its queue configuration and lifecycle status | consumer (event messages), admitter (status transitions) | Never. Terminal statuses are kept. |
| `alembic_version` | Alembic's current revision (created by Alembic) | migrate task | No |

Runtime state (waiting queues, active pools, admissions) is **not** in MySQL; it lives in Redis. See "Redis design" in `CLAUDE.md`.

## Conventions

- **All timestamps are UTC**, stored as naive `DATETIME(3)` (millisecond precision). The app converts them to and from timezone-aware UTC datetimes (`UTCDateTime` in `app/core/db.py`) and refuses to write naive values.
- `created_at` and `updated_at` are filled by MySQL's `CURRENT_TIMESTAMP(3)`, which follows the **session** time zone. The session must therefore run in UTC (dev/test compose sets `--default-time-zone=+00:00`; for RDS see Phase 10.4).
- IDs are UUID strings in `CHAR(36)`.

## DDL (effective, after all migrations)

```sql
CREATE TABLE queue_configs (
  event_id             CHAR(36)     NOT NULL,
  event_version        INT          NOT NULL,
  total_capacity       INT          NOT NULL,
  active_pool_capacity INT          NOT NULL,
  queue_opens_at       DATETIME(3)  NOT NULL,
  release_at           DATETIME(3)  NOT NULL,
  status               ENUM('SCHEDULED','OPEN','ADMITTING','CLOSED','CANCELLED') NOT NULL,
  closed_at            DATETIME(3)  NULL,
  created_at           DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at           DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  sale_ends_at         DATETIME(3)  NULL,                -- added by 0002, so it's the last column
  PRIMARY KEY (event_id),
  INDEX idx_status_release (status, release_at),
  INDEX idx_status_opens (status, queue_opens_at)      -- added by 0005
);
```

## `queue_configs`

One row per event, created by `EventPublished`. Rows are never deleted.

| Column | Type | Null | Meaning |
| --- | --- | --- | --- |
| `event_id` | CHAR(36) | No | PK. The Event service's event ID. |
| `event_version` | INT | No | Version of the last applied event message. Messages with `event_version <=` this are ignored. |
| `total_capacity` | INT | No | Tickets for the event, from EventPublished/EventUpdated. |
| `active_pool_capacity` | INT | No | Derived: `max(1, floor(total_capacity / 5))`. Recomputed whenever `total_capacity` changes. |
| `queue_opens_at` | DATETIME(3) | No | When buyers can start joining. Sent by Event as `queue_join_opens_at`; at most `release_at`. |
| `release_at` | DATETIME(3) | No | When admission starts. |
| `status` | ENUM | No | Persisted lifecycle status (see below). |
| `closed_at` | DATETIME(3) | Yes | When the queue became CLOSED or CANCELLED. For a sale end, it equals `sale_ends_at`. |
| `created_at` | DATETIME(3) | No | Row creation (MySQL default). |
| `updated_at` | DATETIME(3) | No | Last change (MySQL `ON UPDATE`). |
| `sale_ends_at` | DATETIME(3) | Yes | Optional end of sale; must be after `release_at`. Null means no end. |

**Status.** OPEN and ADMITTING are derived from time by `effective_status(config, now)`. The stored `status` can lag by about one admitter tick, and it's what `GET /internal/v1/queues?status=` filters on.

| Status | Set by | Terminal |
| --- | --- | --- |
| SCHEDULED | consumer, on EventPublished | No |
| OPEN | admitter, at `queue_opens_at` | No |
| ADMITTING | admitter, at `release_at` (after the shuffle) | No |
| CLOSED | consumer on EventSoldOut, or admitter when `sale_ends_at` passes | Yes |
| CANCELLED | consumer, on EventCancelled | Yes |

Status only moves forward. Event messages lock the row (`SELECT … FOR UPDATE`) before changing it.

**Index `idx_status_release (status, release_at)`** serves the internal queue listing (filter by status, ordered by release time).

**Index `idx_status_opens (status, queue_opens_at)`** serves the admitter's lookup of events whose queue has opened.

## Migration history

| Revision | Date | Change |
| --- | --- | --- |
| `0001` | 2026-10-14 | Create `queue_configs` (with `idx_status_release`) and `processed_messages` |
| `0002` | 2026-10-17 | Add nullable `queue_configs.sale_ends_at` |
| `0003` | 2026-10-17 | Add `idx_processed_at` on `processed_messages.processed_at` |
| `0005` | 2026-10-28 | Add `idx_status_opens (status, queue_opens_at)`: `queue_opens_at` now comes from Event's `queue_join_opens_at` instead of `release_at - 15 min`, so the admitter looks events up by it |
| `0004` | 2026-10-21 | Drop `processed_messages` (and its index). It was only an audit record; nothing read it, and duplicates are already harmless (`event_version` checks, forward-only status, the release script's not-found case). |

Apply with `alembic upgrade head` (locally `uv run alembic upgrade head`; on AWS the one-off migrate task).

## Database users (AWS)

| User | Grants | Used by |
| --- | --- | --- |
| `queue_app` | `SELECT, INSERT, UPDATE, DELETE` on `queue_service.*` | api, admitter, consumer |
| `queue_migrate` | `ALL PRIVILEGES` on `queue_service.*` | migrate task only |

Both are created with `REQUIRE SSL`. The full SQL is in Phase 10.4 of `docs/implementation-plan.md`.
