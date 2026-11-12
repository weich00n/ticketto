"""The admitter: one tick about once a second. Run exactly one instance.

Each tick:
  1. Promote: SCHEDULED events whose lobby has opened become OPEN (persisted), and
     every OPEN/ADMITTING event is (re)added to queue:live_events.
  2. For each live event: at release, shuffle the pre-queue once and persist
     ADMITTING; then sweep expired admissions and fill free slots.
  3. For each draining (sold-out) event: sweep only, and stop once the pool is empty.
  4. Write the heartbeat.

Failures are isolated per event: one broken event never stops the others. The
scripts are atomic, so ticks racing OrderConfirmed admits are safe; running two
admitters is still unsupported, because shuffle timing and logs assume one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import datetime
from random import Random

from redis.asyncio import Redis

from app.core.redis import server_now
from app.core.status import TERMINAL_STATUSES, QueueStatus, effective_status
from app.core.timefmt import to_ms
from app.queue import keys
from app.queue.config_repo import ConfigRepository, QueueConfigData
from app.queue.repository import QueueRepository

log = logging.getLogger(__name__)

HEARTBEAT_TTL_MS = 10_000
SLOW_TICK_FRACTION = 0.8  # warn when a tick uses more than 80% of its interval


@dataclass
class TickReport:
    """What one tick did (for tests and logs)."""

    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)  # sale_ends_at passed this tick
    shuffled: list[str] = field(default_factory=list)
    admitted: dict[str, int] = field(default_factory=dict)  # event_id -> new admissions
    released: dict[str, int] = field(default_factory=dict)  # event_id -> expired, swept
    dropped_live: list[str] = field(default_factory=list)  # terminal events left the work list
    drained: list[str] = field(default_factory=list)  # draining events now empty
    failed: list[str] = field(default_factory=list)  # event_ids whose processing raised


class Admitter:
    def __init__(
        self,
        configs: ConfigRepository,
        queues: QueueRepository,
        redis: Redis,
        *,
        tick_seconds: float = 1.0,
        shuffle_rng: Random | None = None,
    ):
        self._configs = configs
        self._queues = queues
        self._redis = redis
        self.tick_seconds = tick_seconds
        self._shuffle_rng = shuffle_rng  # None: secrets.SystemRandom (production)

    # --- one tick ------------------------------------------------------------------

    async def tick(self) -> TickReport:
        report = TickReport()
        now = await server_now(self._redis)
        await self._promote(now, report)
        for event_id in await self._queues.live_event_ids():
            await self._isolated(event_id, report, self._process_live(event_id, now, report))
        for event_id in await self._queues.draining_event_ids():
            await self._isolated(event_id, report, self._process_draining(event_id, report))
        await self._redis.set(keys.ADMITTER_HEARTBEAT, to_ms(now), px=HEARTBEAT_TTL_MS)
        return report

    async def _promote(self, now: datetime, report: TickReport) -> None:
        events = await self._configs.live_or_due(now)
        for cfg in events:
            if (
                cfg.status is QueueStatus.SCHEDULED
                and effective_status(cfg, now) not in TERMINAL_STATUSES  # sale not over
                and await self._configs.set_status(cfg.event_id, QueueStatus.OPEN)
            ):
                report.opened.append(cfg.event_id)
                log.info("queue opened", extra={"event_id": cfg.event_id})
        await self._queues.add_live(*(cfg.event_id for cfg in events))

    async def _process_live(self, event_id: str, now: datetime, report: TickReport) -> None:
        cfg = await self._configs.get(event_id)
        if cfg is None:
            log.warning("live event has no config; dropping", extra={"event_id": event_id})
            await self._queues.remove_live(event_id)
            report.dropped_live.append(event_id)
            return
        status = effective_status(cfg, now)
        if status in TERMINAL_STATUSES:
            if cfg.status not in TERMINAL_STATUSES:
                # Only the clock says CLOSED: sale_ends_at has passed. Close it
                # now, before any admit in this tick.
                await self._close_for_sale_end(cfg, report)
            else:
                # The EventSoldOut/EventCancelled handler normally did this already.
                await self._queues.remove_live(event_id)
                report.dropped_live.append(event_id)
            return
        if status is not QueueStatus.ADMITTING:
            return  # OPEN: buyers gather in the pre-queue until release

        if not await self._queues.is_shuffled(event_id):
            shuffle = await self._queues.shuffle(cfg, self._shuffle_rng)
            if shuffle.performed:
                report.shuffled.append(event_id)
                log.info(
                    "pre-queue shuffled",
                    extra={
                        "event_id": event_id,
                        "ordered": shuffle.ordered,
                        "leftovers": shuffle.leftovers,
                    },
                )
        if cfg.status is not QueueStatus.ADMITTING:
            # Idempotent, and refused if the event went terminal meanwhile.
            await self._configs.set_status(event_id, QueueStatus.ADMITTING)

        result = await self._queues.admit(cfg)
        if result.admitted:
            report.admitted[event_id] = len(result.admitted)
        if result.released:
            report.released[event_id] = len(result.released)
        if result.admitted or result.released:
            log.info(
                "admit",
                extra={
                    "event_id": event_id,
                    "admitted": len(result.admitted),
                    "released": len(result.released),
                },
            )

    async def _close_for_sale_end(self, cfg: QueueConfigData, report: TickReport) -> None:
        """Same as EventSoldOut, with closed_at = sale_ends_at: waiting buyers are
        cleared; admitted buyers keep their admission and the pool drains."""
        event_id = cfg.event_id
        if await self._configs.set_status(event_id, QueueStatus.CLOSED, closed_at=cfg.sale_ends_at):
            await self._queues.close_event(event_id)
            report.closed.append(event_id)
            log.info("sale ended; queue closed", extra={"event_id": event_id})
        else:
            # Went terminal some other way meanwhile (e.g. cancelled); that path cleans up.
            await self._queues.remove_live(event_id)
            report.dropped_live.append(event_id)

    async def _process_draining(self, event_id: str, report: TickReport) -> None:
        cfg = await self._configs.get(event_id)
        if cfg is not None:
            # Status is terminal, so this only sweeps expired admissions.
            result = await self._queues.admit(cfg)
            if result.released:
                report.released[event_id] = len(result.released)
        if cfg is None or await self._queues.pool_size(event_id) == 0:
            await self._queues.remove_draining(event_id)
            report.drained.append(event_id)
            log.info("drained", extra={"event_id": event_id})

    async def _isolated(self, event_id: str, report: TickReport, work: Awaitable[None]) -> None:
        try:
            await work
        except Exception:
            report.failed.append(event_id)
            log.exception("admitter failed for event", extra={"event_id": event_id})

    # --- the loop ------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Tick until `stop` is set. The tick in progress always finishes."""
        log.info("admitter started", extra={"tick_seconds": self.tick_seconds})
        while not stop.is_set():
            started = time.monotonic()
            try:
                await self.tick()
            except Exception:
                # Redis/MySQL down, etc.: keep going, the next tick retries.
                log.exception("admitter tick failed")
            elapsed = time.monotonic() - started
            if elapsed > SLOW_TICK_FRACTION * self.tick_seconds:
                log.warning(
                    "slow admitter tick",
                    extra={"elapsed_seconds": round(elapsed, 3), "tick_seconds": self.tick_seconds},
                )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=max(0.0, self.tick_seconds - elapsed))
        log.info("admitter stopped")
