"""The admitter's loop mechanics, with tick() replaced (no Redis/MySQL):
pacing, graceful stop, surviving failures, slow-tick warnings, per-event isolation."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, cast

import pytest

from app.queue.admitter import Admitter, TickReport

pytestmark = pytest.mark.unit


class ScriptedAdmitter(Admitter):
    def __init__(self, tick_seconds: float, behaviour: Any = None) -> None:
        super().__init__(
            cast(Any, None), cast(Any, None), cast(Any, None), tick_seconds=tick_seconds
        )
        self.ticks: list[float] = []
        self.behaviour = behaviour

    async def tick(self) -> TickReport:
        self.ticks.append(time.monotonic())
        if self.behaviour is not None:
            await self.behaviour(self)
        return TickReport()


async def _run_for(admitter: Admitter, seconds: float) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(admitter.run(stop))
    await asyncio.sleep(seconds)
    stop.set()
    await asyncio.wait_for(task, timeout=2)


async def test_ticks_at_the_configured_interval() -> None:
    admitter = ScriptedAdmitter(tick_seconds=0.05)
    await _run_for(admitter, 0.32)
    assert 5 <= len(admitter.ticks) <= 8
    gaps = [b - a for a, b in zip(admitter.ticks, admitter.ticks[1:], strict=False)]
    assert all(gap >= 0.04 for gap in gaps)


async def test_stop_lets_the_current_tick_finish() -> None:
    finished: list[bool] = []
    stop = asyncio.Event()

    async def slow(_: ScriptedAdmitter) -> None:
        stop.set()  # stop requested mid-tick
        await asyncio.sleep(0.05)
        finished.append(True)

    admitter = ScriptedAdmitter(tick_seconds=10, behaviour=slow)
    await asyncio.wait_for(admitter.run(stop), timeout=2)

    assert finished == [True]
    assert len(admitter.ticks) == 1  # stopped promptly, didn't wait 10 s


async def test_failing_tick_does_not_stop_the_loop(caplog: pytest.LogCaptureFixture) -> None:
    async def flaky(admitter: ScriptedAdmitter) -> None:
        if len(admitter.ticks) == 1:
            raise ConnectionError("redis down")

    admitter = ScriptedAdmitter(tick_seconds=0.02, behaviour=flaky)
    with caplog.at_level(logging.ERROR):
        await _run_for(admitter, 0.15)

    assert len(admitter.ticks) >= 3
    assert "admitter tick failed" in caplog.text


async def test_slow_tick_is_warned(caplog: pytest.LogCaptureFixture) -> None:
    async def slow(_: ScriptedAdmitter) -> None:
        await asyncio.sleep(0.05)

    admitter = ScriptedAdmitter(tick_seconds=0.05, behaviour=slow)
    with caplog.at_level(logging.WARNING):
        await _run_for(admitter, 0.12)

    assert "slow admitter tick" in caplog.text


async def test_event_failure_is_isolated(caplog: pytest.LogCaptureFixture) -> None:
    admitter = ScriptedAdmitter(tick_seconds=1)
    report = TickReport()
    done: list[str] = []

    async def boom() -> None:
        raise ValueError("corrupt config")

    async def fine() -> None:
        done.append("b")

    with caplog.at_level(logging.ERROR):
        await admitter._isolated("event-a", report, boom())
        await admitter._isolated("event-b", report, fine())

    assert report.failed == ["event-a"]
    assert done == ["b"]
    assert "admitter failed for event" in caplog.text
