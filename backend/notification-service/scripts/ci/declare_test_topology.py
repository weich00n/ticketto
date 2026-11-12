"""CI only: give a stock RabbitMQ the part of the Ticketto topology notification-service uses.

In AWS and in Compose the broker is rabbitmq-service's image, which loads the whole topology
(topology/definitions.json) at boot. CI runs a stock image as a GitLab service, so this
declares the same exchanges, queues, arguments and bindings for notification-service's
three queues. The delivery limit is a policy on the real broker; here it's a queue argument.
"""

import os
import sys
import time

import pika

URL = os.environ.get("IT_RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/%2F")
CONSUMER = "notification-service"
MESSAGES = {  # message -> (exchange, routing key), as in topology/messages.yaml
    "EventPublished": ("event.events", "event.published"),
    "EventCancelled": ("event.events", "event.cancelled"),
    "OrderRefunded": ("checkout.events", "order.refunded"),
    "RefundFailed": ("checkout.events", "refund.failed"),
    "TicketIssued": ("ticket.events", "ticket.issued"),
}


def connect() -> pika.BlockingConnection:
    for _ in range(60):
        try:
            return pika.BlockingConnection(pika.URLParameters(URL))
        except pika.exceptions.AMQPError:
            time.sleep(2)
    sys.exit(f"broker not reachable at {URL}")


def main() -> None:
    connection = connect()
    channel = connection.channel()
    channel.exchange_declare("ticketto.dlx", exchange_type="direct", durable=True)
    for exchange in {e for e, _ in MESSAGES.values()}:
        channel.exchange_declare(exchange, exchange_type="topic", durable=True)
    for message, (exchange, routing_key) in MESSAGES.items():
        queue = f"{CONSUMER}.{message}"
        channel.queue_declare(f"{queue}.dlq", durable=True, arguments={"x-queue-type": "quorum"})
        channel.queue_bind(f"{queue}.dlq", "ticketto.dlx", routing_key=queue)
        channel.queue_declare(
            queue,
            durable=True,
            arguments={
                "x-queue-type": "quorum",
                "x-dead-letter-exchange": "ticketto.dlx",
                "x-dead-letter-routing-key": queue,
                "x-dead-letter-strategy": "at-least-once",
                "x-overflow": "reject-publish",
                "x-delivery-limit": 5,
            },
        )
        channel.queue_bind(queue, exchange, routing_key=routing_key)
    connection.close()
    print(f"declared {len(MESSAGES)} {CONSUMER} queues and their DLQs")


if __name__ == "__main__":
    main()
