"""Load smoke test against a running stack (api + admitter + consumer).

    make up-app          # or: make up, make migrate, then make api/admitter/consumer
    uv run python -m scripts.load_smoke --buyers 2000 --release-in 30 --duration 120

What it does:
  1. Publishes an EventPublished (via RabbitMQ, like the Event service) whose
     release is --release-in seconds away, so the lobby opens at once.
  2. --buyers buyers join the pre-queue (bounded by --concurrency).
  3. Every buyer polls GET /queue/me at the server-directed poll_after_seconds.
  4. Admitted buyers "check out" after 1-3 s: an OrderConfirmed is published,
     freeing their slot (turn off with --no-orders to test the 11-minute sweep).
  5. After --duration seconds, or once everyone has been admitted, it prints
     latency percentiles per endpoint, error counts and how many were admitted.

Not run in CI. The plan's target is 20k buyers with p99 /queue/me well under
100 ms; the admitter warns in its log if a tick takes longer than 0.8 s.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import statistics
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pika


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; 0.0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


@dataclass
class Stats:
    latencies: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    statuses: dict[str, dict[int, int]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(int))
    )
    errors: int = 0
    admitted: set[str] = field(default_factory=set)
    ordered: set[str] = field(default_factory=set)

    def record(self, endpoint: str, seconds: float, status: int) -> None:
        self.latencies[endpoint].append(seconds * 1000)
        self.statuses[endpoint][status] += 1

    def report(self) -> str:
        lines = [
            f"{'endpoint':10} {'count':>7} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8}  statuses"
        ]
        for endpoint, values in sorted(self.latencies.items()):
            codes = dict(sorted(self.statuses[endpoint].items()))
            lines.append(
                f"{endpoint:10} {len(values):7d} "
                + " ".join(f"{percentile(values, p):7.1f}ms" for p in (50, 95, 99))
                + f" {max(values):7.1f}ms  {codes}"
            )
        lines.append(f"transport errors: {self.errors}")
        lines.append(f"admitted: {len(self.admitted)}, orders confirmed: {len(self.ordered)}")
        return "\n".join(lines)


class Publisher:
    """One pika channel shared across coroutines (via a thread and a lock)."""

    def __init__(self, url: str):
        self._connection = pika.BlockingConnection(pika.URLParameters(url))
        self._channel = self._connection.channel()
        self._channel.confirm_delivery()
        self._lock = threading.Lock()

    def publish(
        self,
        exchange: str,
        routing_key: str,
        message_type: str,
        producer: str,
        payload: dict[str, Any],
    ) -> None:
        message_id = str(uuid.uuid4())
        envelope = {
            "message_id": message_id,
            "message_type": message_type,
            "schema_version": 1,
            "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "producer": producer,
            "payload": payload,
        }
        with self._lock:
            self._channel.basic_publish(
                exchange,
                routing_key,
                json.dumps(envelope).encode(),
                properties=pika.BasicProperties(
                    content_type="application/json",
                    delivery_mode=2,
                    message_id=message_id,
                    type=message_type,
                ),
            )

    def close(self) -> None:
        self._connection.close()


async def buyer(
    client: httpx.AsyncClient,
    publisher: Publisher,
    event_id: str,
    stats: Stats,
    deadline: float,
    join_gate: asyncio.Semaphore,
    confirm_orders: bool,
) -> None:
    user = str(uuid.uuid4())
    headers = {"X-User-Sub": user}
    base = f"/api/v1/events/{event_id}/queue"
    async with join_gate:
        body = await _request(client, stats, "join", "POST", f"{base}/entries", headers)
    while body is not None and time.monotonic() < deadline:
        if body.get("entry_status") == "ADMITTED":
            stats.admitted.add(user)
            if confirm_orders:
                await asyncio.sleep(random.uniform(1, 3))  # choosing seats, paying
                await asyncio.to_thread(
                    publisher.publish,
                    "checkout.events",
                    "order.confirmed",
                    "OrderConfirmed",
                    "load-smoke",
                    {
                        "order_id": str(uuid.uuid4()),
                        "admission_id": body["admission"]["admission_id"],
                        "user_id": user,
                        "event_id": event_id,
                        "confirmed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    },
                )
                stats.ordered.add(user)
            return
        wait = body.get("poll_after_seconds")
        if wait is None:  # NONE / closed: stop polling
            return
        await asyncio.sleep(wait)
        body = await _request(client, stats, "me", "GET", f"{base}/me", headers)


async def _request(
    client: httpx.AsyncClient,
    stats: Stats,
    endpoint: str,
    method: str,
    url: str,
    headers: dict[str, str],
) -> dict[str, Any] | None:
    started = time.monotonic()
    try:
        resp = await client.request(method, url, headers=headers)
    except httpx.HTTPError:
        stats.errors += 1
        return None
    stats.record(endpoint, time.monotonic() - started, resp.status_code)
    if resp.status_code >= 400:
        return None
    data: dict[str, Any] = resp.json()
    return data


async def run(args: argparse.Namespace) -> Stats:
    stats = Stats()
    event_id = str(uuid.uuid4())
    publisher = Publisher(args.rabbitmq_url)
    release_at = datetime.now(UTC) + timedelta(seconds=args.release_in)
    publisher.publish(
        "event.events",
        "event.published",
        "EventPublished",
        "load-smoke",
        {
            "event_id": event_id,
            "event_version": 1,
            "total_capacity": args.capacity,
            "release_at": release_at.isoformat().replace("+00:00", "Z"),
        },
    )
    limits = httpx.Limits(
        max_connections=args.concurrency, max_keepalive_connections=args.concurrency
    )
    async with httpx.AsyncClient(base_url=args.api, limits=limits, timeout=10) as client:
        for _ in range(100):  # wait for the consumer to create the queue
            resp = await client.get(f"/api/v1/events/{event_id}/queue")
            if resp.status_code == 200:
                break
            await asyncio.sleep(0.2)
        else:
            raise SystemExit("event never appeared: is the consumer running?")
        print(
            f"event {event_id}: lobby {resp.json()['queue_status']}, release in {args.release_in}s"
        )

        deadline = time.monotonic() + args.duration
        gate = asyncio.Semaphore(args.concurrency)
        await asyncio.gather(
            *(
                buyer(client, publisher, event_id, stats, deadline, gate, not args.no_orders)
                for _ in range(args.buyers)
            )
        )
        detail = (await client.get(f"/internal/v1/events/{event_id}/queue")).json()
        final = {k: detail.get(k) for k in ("queue_status", "waiting_count", "active_count")}
        print(f"final queue state: {json.dumps(final)}")
    publisher.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--rabbitmq-url", default="amqp://queue:queue@localhost:5672/%2F")
    parser.add_argument("--buyers", type=int, default=2000)
    parser.add_argument("--capacity", type=int, default=304, help="total_capacity (pool = /5)")
    parser.add_argument("--release-in", type=int, default=30)
    parser.add_argument("--duration", type=int, default=120)
    parser.add_argument("--concurrency", type=int, default=200)
    parser.add_argument("--no-orders", action="store_true", help="never confirm orders")
    args = parser.parse_args(argv)
    started = time.monotonic()
    stats = asyncio.run(run(args))
    print(stats.report())
    print(
        f"wall time: {time.monotonic() - started:.1f}s, mean join latency: "
        f"{statistics.fmean(stats.latencies['join']) if stats.latencies['join'] else 0:.1f}ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
