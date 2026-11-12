"""Background process: consumes user-service's RabbitMQ queues.

Runs from the same image as the API: `python -m app.worker`.
  user-service.OrderConfirmed  add the event to the buyer's purchased events
  user-service.EventCancelled  record the cancellation, so a late order isn't added
SIGTERM finishes the message in progress and exits. Exits non-zero if a consumer thread
crashed, so ECS restarts the task.
"""

import logging
import signal
import sys
import threading

from app.config import get_settings
from app.db import get_sessionmaker
from app.logging_config import configure_logging
from app.messaging.consumer import start_consumers
from app.messaging.handlers import handle_message

logger = logging.getLogger(__name__)


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    if not settings.rabbitmq_url:
        logger.error("RABBITMQ_URL is not set")
        return 2
    session_factory = get_sessionmaker()

    stop = threading.Event()
    crashed = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    threads = start_consumers(
        settings.rabbitmq_url, lambda m: handle_message(session_factory, m), stop, crashed
    )
    logger.info("worker started")
    while not stop.is_set():
        stop.wait(1)
    for thread in threads:
        thread.join(timeout=10)
    logger.info("worker stopped", extra={"crashed": crashed.is_set()})
    return 1 if crashed.is_set() else 0


if __name__ == "__main__":
    sys.exit(main())
