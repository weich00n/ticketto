"""The standalone checkout worker: `python -m app.worker`.

One process runs everything that is not HTTP:

  outbox relay     every ~2s   publish undelivered outbox rows to checkout.events
  deadline sweep   every ~10s  expire orders past their payment deadline
  confirm retry    every ~10s  re-attempt reservation confirm for stuck CONFIRMING orders
  refund processor every ~30s  issue and finalise outstanding refund obligations
  consumers        3 threads   drain the checkout-service.* broker queues

It is a separate process from the API (same image, different command), matching
event-service and queue-service: an API redeploy never drops an in-flight
message, and the worker can be restarted or scaled independently. SIGTERM stops
all loops and drains the consumers before exiting, so ECS can roll the task
without losing acknowledged work.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import UTC, datetime

from . import config as app_config
from .clients.reservation import HttpReservationClient
from .clients.stripe import HttpStripeClient
from .db import create_session_factory
from .logging_config import configure_logging
from .messaging.consumers import start_consumers
from .messaging.handlers import MessageHandlers
from .messaging.outbox import Outbox
from .service import CheckoutService

log = logging.getLogger(__name__)

RELAY_INTERVAL = 2.0
SWEEP_INTERVAL = 10.0
REFUND_INTERVAL = 30.0


def _sleep(stop: threading.Event, seconds: float) -> None:
    stop.wait(seconds)


def run_worker(session_factory, outbox, service, stop) -> None:
    """The fast relay loop plus, on their own cycles, the slower sweeps."""
    last_sweep = 0.0
    last_refund = 0.0
    log.info("worker started")
    while not stop.is_set():
        try:
            with session_factory() as db:
                sent = outbox.deliver_pending(db)
            if sent:
                log.info("outbox: delivered", extra={"count": sent})
        except Exception:  # pylint: disable=broad-exception-caught  # keep the loop alive
            log.exception("outbox relay cycle failed")

        now = time.monotonic()
        if now - last_sweep >= SWEEP_INTERVAL:
            last_sweep = now
            try:
                expired = service.expire_stale_orders(datetime.now(UTC))
                if expired:
                    log.info("deadline sweep: expired", extra={"count": expired})
            except Exception:  # pylint: disable=broad-exception-caught  # keep the loop alive
                log.exception("deadline sweep cycle failed")
            try:
                done = service.retry_confirms()
                if done:
                    log.info("confirm retry: advanced", extra={"count": done})
            except Exception:  # pylint: disable=broad-exception-caught  # keep the loop alive
                log.exception("confirm retry cycle failed")

        if now - last_refund >= REFUND_INTERVAL:
            last_refund = now
            try:
                processed = service.process_refunds()
                if processed:
                    log.info("refund processor: handled", extra={"count": processed})
            except Exception:  # pylint: disable=broad-exception-caught  # keep the loop alive
                log.exception("refund processor cycle failed")

        _sleep(stop, RELAY_INTERVAL)


def main() -> None:
    settings = app_config.Settings.from_environment()
    configure_logging(settings.log_level)

    session_factory = create_session_factory(settings)
    outbox = Outbox(settings)
    reservation = HttpReservationClient(settings.reservation_base_url)
    stripe = HttpStripeClient(settings.stripe_api_key, settings.stripe_webhook_secret)
    service = CheckoutService(settings, session_factory, outbox, reservation, stripe)
    handlers = MessageHandlers(session_factory, service)

    stop = threading.Event()
    crashed = threading.Event()

    def _handle(signum, _frame) -> None:
        log.info("signal %s received; stopping", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    threads = start_consumers(settings, handlers, stop, crashed)
    log.info(
        "checkout worker running",
        extra={
            "queues": list(settings.broker_consumer_queues),
            "relay_seconds": RELAY_INTERVAL,
            "sweep_seconds": SWEEP_INTERVAL,
            "refund_seconds": REFUND_INTERVAL,
        },
    )

    run_worker(session_factory, outbox, service, stop)

    log.info("worker stopped; draining consumers")
    for t in threads:
        t.join(timeout=15)
    if crashed.is_set():
        log.error("a consumer crashed; exiting non-zero so the orchestrator restarts")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
