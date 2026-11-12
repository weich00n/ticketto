"""Send the messages Checkout and Event Service would send, to try Ticket Service by hand.

Publishes to the exchanges and routing keys in the team's broker catalogue
(rabbitmq-service/topology/messages.yaml), as the broker's admin user: Ticket's own user may
not publish to Checkout's or Event's exchange. Run it with the project's virtualenv active
(it needs pika) while the stack is running:

    python scripts/dev/send_test_message.py order --quantity 2
    python scripts/dev/send_test_message.py cancel --event <event_id>

Each command prints `export NAME=value` lines on stdout (messages go to stderr), so
eval "$(python scripts/dev/send_test_message.py order)" sets ORDER_ID, EVENT_ID, USER_ID
and CATEGORY_ID in your shell for the next commands.
"""

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pika
from pika.exceptions import AMQPConnectionError, ChannelClosedByBroker, UnroutableError

# The local broker's admin user (docker-compose.yml); there is no guest user.
DEFAULT_URL = "amqp://ticketto:ticketto-local@localhost:5672/%2F"  # gitleaks:allow
DEFAULT_USER = "demo-buyer"

# message type -> (exchange, routing key, producer), as in the broker's messages.yaml
ROUTES = {
    "OrderConfirmed": ("checkout.events", "order.confirmed", "checkout-service"),
    "RefundCompleted": ("checkout.events", "refund.completed", "checkout-service"),
    "EventPublished": ("event.events", "event.published", "event-service"),
    "EventCancelled": ("event.events", "event.cancelled", "event-service"),
}


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _envelope(message_type: str, payload: dict[str, Any], args) -> dict[str, Any]:
    message_id = args.message_id or str(uuid.uuid4())
    if args.envelope == "checkout":
        # The Technical Contract tab's older spelling, which Ticket also accepts.
        return {
            "message_id": message_id,
            "event_type": message_type,
            "schema_version": 1,
            "occurred_at": _iso(_now()),
            "correlation_id": str(uuid.uuid4()),
            "data": payload,
        }
    return {
        "message_id": message_id,
        "message_type": message_type,
        "schema_version": 1,
        "occurred_at": _iso(_now()),
        "producer": ROUTES[message_type][2],
        "payload": payload,
    }


def _publish(url: str, message_type: str, body: bytes, message_id: str | None) -> None:
    exchange, routing_key, _ = ROUTES[message_type]
    try:
        connection = pika.BlockingConnection(pika.URLParameters(url))
    except AMQPConnectionError:
        sys.exit(f"Cannot connect to RabbitMQ at {url}. Is it running?")
    try:
        channel = connection.channel()
        channel.confirm_delivery()
        channel.basic_publish(
            exchange=exchange,
            routing_key=routing_key,
            body=body,
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=pika.DeliveryMode.Persistent,
                message_id=message_id,
                type=message_type,
                correlation_id=str(uuid.uuid4()),
            ),
            mandatory=True,
        )
    except UnroutableError:
        sys.exit(
            f"No queue is bound to {exchange} for {routing_key}: run broker-setup first "
            "(docker compose up does), which imports Ticket's queues."
        )
    except ChannelClosedByBroker as error:
        sys.exit(f"RabbitMQ refused the message ({error}). Has broker-setup run?")
    finally:
        if connection.is_open:
            connection.close()
    print(f"Sent {message_type} to {exchange} with routing key {routing_key}", file=sys.stderr)


def _send(args, message_type: str, payload: dict[str, Any]) -> None:
    envelope = _envelope(message_type, payload, args)
    if args.show:
        print(json.dumps(envelope, indent=2), file=sys.stderr)
    _publish(args.url, message_type, json.dumps(envelope).encode(), envelope["message_id"])


def _exports(**values: str) -> None:
    for name, value in values.items():
        print(f"export {name}={value}")


def order(args) -> None:
    payload = {
        "order_id": args.order or str(uuid.uuid4()),
        "hold_id": str(uuid.uuid4()),
        "admission_id": str(uuid.uuid4()),
        "user_id": args.user,
        "event_id": args.event or str(uuid.uuid4()),
        "category_id": args.category or str(uuid.uuid4()),
        "quantity": args.quantity,
        "delivery_email": args.email,
        "total_amount": 8800 * args.quantity,
        "currency": "SGD",
        "confirmed_at": _iso(_now()),
    }
    _send(args, "OrderConfirmed", payload)
    _exports(
        ORDER_ID=payload["order_id"],
        EVENT_ID=payload["event_id"],
        CATEGORY_ID=payload["category_id"],
        USER_ID=payload["user_id"],
    )


def event(args) -> None:
    starts = _now() + timedelta(days=30)
    payload = {
        "event_id": args.event or str(uuid.uuid4()),
        "event_version": args.version,
        "title": args.title,
        "venue": args.venue,
        "starts_at": _iso(starts),
        "release_at": _iso(_now()),
        "queue_join_opens_at": _iso(_now()),
        "published_at": _iso(_now()),
        "total_capacity": 300,
        "ticket_categories": [
            {
                "category_id": args.category or str(uuid.uuid4()),
                "name": args.category_name,
                "price_amount": 8800,
                "currency": "SGD",
            }
        ],
    }
    _send(args, "EventPublished", payload)
    _exports(
        EVENT_ID=payload["event_id"], CATEGORY_ID=payload["ticket_categories"][0]["category_id"]
    )


def cancel(args) -> None:
    payload = {
        "event_id": args.event,
        "event_version": args.version,
        "cancellation_id": str(uuid.uuid4()),
        "cancelled_at": _iso(_now()),
        "reason": args.reason,
    }
    _send(args, "EventCancelled", payload)


def refund(args) -> None:
    payload = {
        "order_id": args.order,
        "event_id": args.event,
        "refund_id": str(uuid.uuid4()),
        "amount": 8800,
        "currency": "SGD",
    }
    _send(args, "RefundCompleted", payload)


def poison(args) -> None:
    # Not an envelope at all: Ticket must dead-letter it straight away.
    _publish(args.url, "OrderConfirmed", b'{"hello": "not a valid message"}', None)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", maxsplit=1)[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--url",
        default=os.environ.get("BROKER_ADMIN_URL", DEFAULT_URL),
        help="AMQP URL of a user allowed to publish there "
        "(default: $BROKER_ADMIN_URL, else the local admin user)",
    )
    common.add_argument(
        "--envelope",
        choices=["broker", "checkout"],
        default="broker",
        help="broker: message_type/payload (rabbitmq-service contract, what Checkout sends). "
        "checkout: event_type/data, the Technical Contract tab's older spelling",
    )
    common.add_argument("--message-id", help="reuse a message_id to simulate a redelivery")
    common.add_argument("--show", action="store_true", help="print the message sent")
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("order", parents=[common], help="send OrderConfirmed")
    p.add_argument("--user", default=os.environ.get("USER_ID", DEFAULT_USER))
    p.add_argument("--event", help="event_id (default: a new one)")
    p.add_argument("--category", help="category_id (default: a new one)")
    p.add_argument("--order", help="order_id (default: a new one; reuse to test duplicates)")
    p.add_argument("--quantity", type=int, default=2)
    p.add_argument("--email", default="buyer@example.com")
    p.set_defaults(handler=order)

    p = commands.add_parser("event", parents=[common], help="send EventPublished")
    p.add_argument("--event", help="event_id (default: a new one)")
    p.add_argument("--category", help="category_id (default: a new one)")
    p.add_argument("--category-name", default="CAT 1")
    p.add_argument("--title", default="The Midnight Symphony")
    p.add_argument("--venue", default="Victoria Concert Hall")
    p.add_argument("--version", type=int, default=1)
    p.set_defaults(handler=event)

    p = commands.add_parser("cancel", parents=[common], help="send EventCancelled")
    p.add_argument("--event", required=True)
    p.add_argument("--version", type=int, default=2)
    p.add_argument("--reason", default="Venue closure")
    p.set_defaults(handler=cancel)

    p = commands.add_parser("refund", parents=[common], help="send RefundCompleted")
    p.add_argument("--order", required=True)
    p.add_argument("--event", required=True)
    p.set_defaults(handler=refund)

    p = commands.add_parser("poison", parents=[common], help="send a malformed OrderConfirmed")
    p.set_defaults(handler=poison)
    return parser


if __name__ == "__main__":
    parsed = _parser().parse_args()
    parsed.handler(parsed)
