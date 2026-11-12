"""Background loop: marks finished events COMPLETED and delivers outbox messages."""

import logging
import signal
import time

from .config import Settings
from .db import create_session_factory
from .lifecycle import complete_past_events
from .logging_config import configure_logging
from .outbox import publish_pending_messages

logger = logging.getLogger(__name__)

POLL_SECONDS = 2


def run_once(session_factory, rabbitmq_url: str) -> None:
    try:
        completed = complete_past_events(session_factory)
        if completed:
            logger.info("Events completed", extra={"completed": completed})
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("Completing events failed")
    try:
        delivered = publish_pending_messages(session_factory, rabbitmq_url)
        if delivered:
            logger.info("Outbox messages delivered", extra={"delivered": delivered})
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("Outbox delivery failed")


def main() -> None:
    settings = Settings.from_environment()
    configure_logging(settings.log_level)
    session_factory = create_session_factory(settings)

    stopping = False

    def stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while not stopping:
        run_once(session_factory, settings.rabbitmq_url)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
