"""Check the correctness rules for one event after an experiment.

    python -m checker --event-id <uuid>

Connections (environment):
    RESERVATION_DB_URL     mysql://user:pw@host:3306/reservation_db?ssl=true
    RESERVATION_REDIS_URL  redis://:pw@host:6379/0
    QUEUE_DB_URL           mysql://user:pw@host:3306/queue_service?ssl=true
    QUEUE_REDIS_URL        redis://:pw@host:6379/0

Exit code 0 when every rule holds, 1 when any is violated, so a run script can stop on it.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import redis

from . import collect
from .invariants import check_holds_expired, check_no_oversell, check_slots_freed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="checker", description=__doc__.splitlines()[0])
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--grace-seconds", type=int, default=120,
                        help="how late an expiry, sweep or slot release may be before it counts (default 120)")
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    grace = timedelta(seconds=args.grace_seconds)
    env = os.environ

    reservation_db = collect.mysql(env["RESERVATION_DB_URL"])
    reservation_redis = redis.Redis.from_url(env["RESERVATION_REDIS_URL"])
    queue_db = collect.mysql(env["QUEUE_DB_URL"])
    queue_redis = redis.Redis.from_url(env["QUEUE_REDIS_URL"])

    categories = collect.reservation_categories(reservation_db, reservation_redis, args.event_id)
    if not categories:
        print(f"No inventory for event {args.event_id}: wrong id, or EventPublished never arrived.")
        return 1

    violations = [
        *check_no_oversell(categories),
        *check_holds_expired(collect.reservation_active_holds(reservation_db, args.event_id), now, grace),
        *check_slots_freed(collect.queue_pool(queue_db, queue_redis, args.event_id),
                           collect.reservation_confirmed_admissions(reservation_db, args.event_id), now, grace),
    ]

    print(f"Event {args.event_id} at {now:%Y-%m-%d %H:%M:%S} UTC")
    for c in categories:
        print(f"  category {c.category_id}: total {c.total}, sold {c.sold}, active holds {c.active_qty}, "
              f"Redis available {c.redis_available}")
    if not violations:
        print("PASS: no overselling, no stale holds, queue slots freed")
        return 0
    print(f"FAIL: {len(violations)} violation(s)")
    for v in violations:
        print(f"  {v}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
