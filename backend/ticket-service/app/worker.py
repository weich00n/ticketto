"""Background process: consumes RabbitMQ messages and publishes TicketIssued.

Runs from the same image as the API: `python -m app.worker`. One thread per consumer queue
(the broker owns the queues; Ticket only consumes them); the main thread publishes the outbox
(outbox.py), which notification-service turns into the ticket email. Exits non-zero if a
consumer thread crashes, so ECS restarts the task.
"""

import logging
import signal
import sys
import threading
import time

from .config import Settings
from .consumer import start_consumers
from .db import create_session_factory
from .issuance import prune_processed_messages
from .logging_config import configure_logging
from .outbox import RabbitPublisher, run_once
from .qr import QrSigner

logger = logging.getLogger(__name__)

PRUNE_EVERY_SECONDS = 3600


def main() -> int:
    settings = Settings.from_environment()
    configure_logging(settings.log_level)
    session_factory = create_session_factory(settings)
    signer = QrSigner(settings.qr_signing_keys, settings.qr_active_key_id)
    publisher = RabbitPublisher(settings.rabbitmq_url)

    stop = threading.Event()
    crashed = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    threads = start_consumers(settings.rabbitmq_url, session_factory, signer, stop, crashed)
    logger.info("worker started")

    last_prune = 0.0
    while not stop.is_set():
        try:
            run_once(session_factory, settings, publisher)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("outbox publishing cycle failed")
        publisher.keepalive()
        if time.monotonic() - last_prune >= PRUNE_EVERY_SECONDS:
            last_prune = time.monotonic()
            try:
                pruned = prune_processed_messages(session_factory)
                if pruned:
                    logger.info("old processed-message records pruned", extra={"pruned": pruned})
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("pruning processed messages failed")
        stop.wait(settings.outbox_poll_seconds)

    publisher.close()
    for thread in threads:
        thread.join(timeout=10)
    logger.info("worker stopped", extra={"crashed": crashed.is_set()})
    return 1 if crashed.is_set() else 0


if __name__ == "__main__":
    sys.exit(main())
