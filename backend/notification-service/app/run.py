"""One run of the service: take messages off the queues, then send the emails that are due.

The worker (app/worker.py) calls run_once in a loop, each with a time limit. Taking messages
in is capped at a share of the
time (DRAIN_BUDGET_FRACTION, half by default), so a backlog of messages never stops emails
from going out. Unread messages wait safely on the queue for the next run.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from .broker import QUEUES, drain
from .config import Settings
from .email_delivery import EmailSender, send_due
from .handlers import handle_message, prune_processed_messages

logger = logging.getLogger(__name__)


def _drain_all(settings: Settings, connect, handle, deadline: float, clock) -> dict[str, Any]:
    result: dict[str, Any] = {
        "drained": {},
        "requeued": {},
        "dead_lettered": {},
        "more_to_do": False,
    }
    connection = None
    try:
        connection = connect(settings.rabbitmq_url)
        channel = connection.channel()
        for queue in QUEUES:
            drained = drain(channel, queue, handle, deadline, clock)
            result["drained"][queue] = drained.taken
            if drained.requeued:
                result["requeued"][queue] = drained.requeued
            if drained.dead_lettered:
                result["dead_lettered"][queue] = drained.dead_lettered
            result["more_to_do"] = result["more_to_do"] or drained.hit_deadline
    except Exception as error:  # pylint: disable=broad-exception-caught  # still send emails
        result["broker_error"] = f"{type(error).__name__}: {error}"
        logger.exception("could not drain the queues")
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:  # pylint: disable=broad-exception-caught  # already gone
                logger.warning("closing the broker connection failed")
    return result


def run_once(  # pylint: disable=too-many-arguments  # keyword-only: the run's dependencies
    *,
    settings: Settings,
    session_factory: "sessionmaker[Session]",
    users: Any,
    events: Any,
    sender: EmailSender,
    connect: Callable[[str], Any],
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    start = clock()
    drain_deadline = start + (deadline - start) * settings.drain_budget_fraction

    def handle(message):
        return handle_message(session_factory, users, events, message)

    result = _drain_all(settings, connect, handle, drain_deadline, clock)
    result.update(send_due(session_factory, settings, sender, deadline, clock))
    result["more_to_do"] = result["more_to_do"] or clock() >= deadline
    try:
        result["pruned"] = prune_processed_messages(session_factory)
    except Exception:  # pylint: disable=broad-exception-caught  # next run tries again
        logger.exception("pruning processed messages failed")
    busy = any(result["drained"].values()) or any(result[k] for k in ("sent", "retry", "failed"))
    level = logging.INFO if busy or "broker_error" in result else logging.DEBUG
    logger.log(level, "run finished", extra={"result": result})
    return result
