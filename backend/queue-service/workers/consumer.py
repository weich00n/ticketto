"""Consumer entrypoint: python -m workers.consumer

Declares the RabbitMQ topology (or, with RABBITMQ_DECLARE_TOPOLOGY=false, only checks
that its queues exist), then consumes them, one thread per queue.
SIGTERM/SIGINT finish the message in progress, close connections and exit. If a
consumer thread crashes, the process exits non-zero so it gets restarted.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from types import FrameType

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.messaging.consumers import declare_with_retry, start_consumers

log = logging.getLogger("workers.consumer")


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    stop = threading.Event()
    crashed = threading.Event()

    def on_signal(signum: int, _frame: FrameType | None) -> None:
        log.info("signal %s received; stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    if not declare_with_retry(settings, stop):
        return 0  # stopped before the broker came up
    threads = start_consumers(settings, stop, crashed)
    while not stop.is_set():
        stop.wait(1)  # wake regularly so signals are handled promptly
    for thread in threads:
        thread.join(timeout=10)
    stuck = [t.name for t in threads if t.is_alive()]
    if stuck:
        log.error("consumer threads did not stop in time: %s", stuck)
    log.info("consumer stopped")
    return 1 if crashed.is_set() or stuck else 0


if __name__ == "__main__":
    sys.exit(main())
