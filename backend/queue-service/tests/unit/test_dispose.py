"""The ack / requeue / dead-letter decision for one delivery, without a broker."""

from __future__ import annotations

import logging

import pytest

from app.messaging.consumers import Disposition, dispose, retry_delay
from app.messaging.schemas import Message
from app.messaging.topology import MAX_ATTEMPTS
from tests.fakes import messages

pytestmark = pytest.mark.unit

EVENT = "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90"


def test_handled_message_is_acked() -> None:
    seen: list[Message] = []
    assert dispose(messages.event_sold_out(EVENT).body, seen.append) is Disposition.ACK
    assert seen[0].message_type == "EventSoldOut"


def test_poison_is_dead_lettered_without_running_the_handler(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[Message] = []
    with caplog.at_level(logging.ERROR):
        assert dispose(b"{oops", seen.append) is Disposition.DEAD_LETTER
    assert seen == []
    assert "poison message dead-lettered" in caplog.text


def test_handler_failure_is_requeued(caplog: pytest.LogCaptureFixture) -> None:
    def boom(_: Message) -> None:
        raise ConnectionError("mysql gone away")

    with caplog.at_level(logging.ERROR):
        result = dispose(messages.event_sold_out(EVENT).body, boom, attempt=3)
    assert result is Disposition.REQUEUE
    assert "will be retried" in caplog.text


@pytest.mark.parametrize("attempt", [MAX_ATTEMPTS, MAX_ATTEMPTS + 3])
def test_handler_failure_on_the_last_attempt_is_dead_lettered(
    caplog: pytest.LogCaptureFixture, attempt: int
) -> None:
    """Enforced by the consumer, so it holds even if the queue has no delivery limit."""

    def boom(_: Message) -> None:
        raise ConnectionError("mysql gone away")

    with caplog.at_level(logging.ERROR):
        result = dispose(messages.event_sold_out(EVENT).body, boom, attempt=attempt)
    assert result is Disposition.DEAD_LETTER
    assert "dead-lettered after max attempts" in caplog.text


def test_handler_failure_before_the_last_attempt_is_requeued() -> None:
    def boom(_: Message) -> None:
        raise ConnectionError("mysql gone away")

    body = messages.event_sold_out(EVENT).body
    assert dispose(body, boom, attempt=MAX_ATTEMPTS - 1) is Disposition.REQUEUE


def test_retry_delay_doubles_from_one_second() -> None:
    delays = [retry_delay(attempt) for attempt in range(1, MAX_ATTEMPTS)]
    assert delays == [1, 2, 4, 8]
