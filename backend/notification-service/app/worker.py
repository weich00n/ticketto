"""The background process: takes messages off the queues and sends the emails that are due.

Runs from the same image as the API: `python -m app.worker`. ECS keeps one running.

Each run (app/run.py) has a time limit, then the worker waits and starts another:
  after a normal run     WORKER_POLL_SECONDS
  after a failed run     WORKER_RETRY_SECONDS. A message that fails is requeued, and the broker
                         dead-letters it after 5 deliveries, so retrying quickly would use those
                         up during a short outage of user-service or the database
  after hitting its      no wait: there's a backlog to clear
  time limit

Nothing stops a stuck run, so the worker touches a heartbeat file after every run, and the task
definition's health check replaces the task if the file gets old. SIGTERM finishes the message or
email in hand and exits. Bad configuration exits non-zero, so a bad release fails its deployment.
"""

import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from .broker import connect
from .config import Settings
from .db import create_session_factory
from .email_delivery import EmailSender, build_sender
from .event_client import EventClient
from .logging_config import configure_logging
from .run import run_once
from .user_client import UserClient

logger = logging.getLogger(__name__)

# stopTimeout in deploy/worker-task-definition.json: how long ECS waits after SIGTERM.
STOP_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class AppState:
    settings: Settings
    session_factory: "sessionmaker[Session]"
    users: UserClient
    events: EventClient
    sender: EmailSender
    connect: Any


def build_app(settings: Settings) -> AppState:
    return AppState(
        settings=settings,
        session_factory=create_session_factory(settings),
        users=UserClient(settings.user_service_url, settings.http_timeout_seconds),
        events=EventClient(settings.event_service_url, settings.http_timeout_seconds),
        sender=build_sender(settings),
        connect=connect,
    )


def touch_heartbeat(path: str) -> None:
    try:
        Path(path).touch()
    except OSError:  # the health check will report it: not a reason to stop sending emails
        logger.exception("could not write the heartbeat")


def stoppable_clock(stop: threading.Event) -> Callable[[], float]:
    """A clock that says time's up once stopping. Runs check it before each message and email,
    so SIGTERM ends a run at the next one without either of them being interrupted."""
    return lambda: float("inf") if stop.is_set() else time.monotonic()


def _one_run(app: AppState, stop: threading.Event) -> int:
    """One run. Returns how many seconds to wait before the next."""
    settings = app.settings
    try:
        result = run_once(
            settings=settings,
            session_factory=app.session_factory,
            users=app.users,
            events=app.events,
            sender=app.sender,
            connect=app.connect,
            deadline=time.monotonic() + settings.worker_run_seconds,
            clock=stoppable_clock(stop),
        )
    except Exception:  # pylint: disable=broad-exception-caught  # keep running
        logger.exception("run failed")
        return settings.worker_retry_seconds
    finally:
        touch_heartbeat(settings.worker_heartbeat_file)
    if result["requeued"] or "broker_error" in result:
        return settings.worker_retry_seconds
    return 0 if result.get("more_to_do") else settings.worker_poll_seconds


def run_forever(app: AppState, stop: threading.Event) -> None:
    touch_heartbeat(app.settings.worker_heartbeat_file)  # healthy during a long first run
    while not stop.is_set():
        stop.wait(_one_run(app, stop))


def main() -> int:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    try:
        settings = Settings.from_environment()
    except ValueError as error:  # ConfigError, or a number that isn't one
        logger.error("bad configuration", extra={"error": str(error)})
        return 2
    app = build_app(settings)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    logger.info("worker started")
    run_forever(app, stop)
    logger.info("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
