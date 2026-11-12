"""Inspect and replay dead-lettered messages.

    python -m workers.dlq list                          # depth of every DLQ
    python -m workers.dlq peek EventPublished -n 5      # show messages, leave them
    python -m workers.dlq replay EventPublished         # move all back for retry
    python -m workers.dlq replay EventSoldOut --limit 10

Replay publishes each message straight to its consumer queue (the default
exchange, so no other service sees it again), keeping the body, message_id and
type. The attempt counter starts fresh. Fix the cause first: a poison message
replayed unchanged is dead-lettered again at once.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any

import pika
from pika.adapters.blocking_connection import BlockingChannel

from app.core.config import get_settings
from app.messaging.topology import CONSUMER_QUEUES, ConsumerQueue

# Short names for the CLI: "EventPublished" -> queue-service.EventPublished
QUEUES = {queue.name.removeprefix("queue-service."): queue for queue in CONSUMER_QUEUES}
# Headers RabbitMQ adds on dead-lettering/redelivery; dropped on replay.
_BROKER_HEADERS = {
    "x-death",
    "x-first-death-exchange",
    "x-first-death-queue",
    "x-first-death-reason",
    "x-last-death-exchange",
    "x-last-death-queue",
    "x-last-death-reason",
    "x-delivery-count",
}


@dataclass(frozen=True)
class DeadLetter:
    message_id: str | None
    message_type: str | None
    reason: str | None  # rejected / delivery_limit / ...
    body: bytes


def depths(channel: BlockingChannel) -> dict[str, int]:
    result = {}
    for short, queue in QUEUES.items():
        declared = channel.queue_declare(queue.dlq, passive=True)
        result[short] = int(declared.method.message_count)
    return result


def peek(channel: BlockingChannel, queue: ConsumerQueue, limit: int) -> list[DeadLetter]:
    """Read up to `limit` messages without removing them.

    The messages are put back with a requeue, which on quorum queues moves them to
    the *back* of the DLQ: peeking reorders it. Harmless here (event handlers are
    guarded by event_version), but don't rely on DLQ order after a peek.
    """
    seen: list[DeadLetter] = []
    tags: list[int] = []
    while len(seen) < limit:
        method, properties, body = channel.basic_get(queue.dlq)
        if method is None:
            break
        tags.append(method.delivery_tag)
        headers: dict[str, Any] = properties.headers or {}
        seen.append(
            DeadLetter(
                message_id=properties.message_id,
                message_type=properties.type,
                reason=_str(headers.get("x-first-death-reason")),
                body=body,
            )
        )
    for tag in tags:
        channel.basic_nack(tag, requeue=True)
    return seen


def replay(channel: BlockingChannel, queue: ConsumerQueue, limit: int | None = None) -> int:
    """Move messages from the DLQ back to the consumer queue, oldest first."""
    channel.confirm_delivery()
    moved = 0
    while limit is None or moved < limit:
        method, properties, body = channel.basic_get(queue.dlq)
        if method is None:
            break
        headers = {k: v for k, v in (properties.headers or {}).items() if k not in _BROKER_HEADERS}
        channel.basic_publish(
            "",
            queue.name,
            body,
            properties=pika.BasicProperties(
                content_type=properties.content_type or "application/json",
                delivery_mode=2,
                message_id=properties.message_id,
                type=properties.type,
                headers=headers or None,
            ),
            mandatory=True,
        )
        channel.basic_ack(method.delivery_tag)  # only after the broker confirmed the copy
        moved += 1
    return moved


def _str(value: Any) -> str | None:
    if isinstance(value, bytes):
        return value.decode()
    return None if value is None else str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="depth of every DLQ")
    peek_p = sub.add_parser("peek", help="show messages without removing them")
    peek_p.add_argument("queue", choices=sorted(QUEUES))
    peek_p.add_argument("-n", type=int, default=10)
    replay_p = sub.add_parser("replay", help="move messages back to the consumer queue")
    replay_p.add_argument("queue", choices=sorted(QUEUES))
    replay_p.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    connection = pika.BlockingConnection(pika.URLParameters(get_settings().rabbitmq_url))
    try:
        channel = connection.channel()
        if args.command == "list":
            for name, depth in depths(channel).items():
                print(f"{name:18} {depth}")
        elif args.command == "peek":
            for letter in peek(channel, QUEUES[args.queue], args.n):
                print(f"{letter.message_type} {letter.message_id} reason={letter.reason}")
                print(f"  {letter.body.decode(errors='replace')[:500]}")
        else:
            moved = replay(channel, QUEUES[args.queue], args.limit)
            print(f"replayed {moved} message(s) to {QUEUES[args.queue].name}")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
