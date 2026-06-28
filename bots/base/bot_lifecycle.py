"""Shared bot lifecycle base class.

Every bot is a long-running process that:
- Pings its own heartbeat on a schedule
- Listens on Redis for `panic:execute:<bot_id>` messages
- Checks halt-state before order placement
- Writes to `signals` and `trades` (NEVER reads `scores`; the bot is blind to scoring)
- Closes positions cleanly on /panic or shutdown

This class wires up the cross-bot machinery; per-bot logic goes in subclasses
overriding `iterate()` (and optionally `on_panic()` for venue-specific cleanup).
"""
from __future__ import annotations

import asyncio
import json
import signal as signal_module
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Optional

import redis.asyncio as redis_async
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from framework.audit import write_audit
from framework.config import get_settings
from framework.halt_state import is_bot_halted
from framework.heartbeat import ping
from framework.logging_setup import configure_logging, get_logger


PANIC_CHANNEL_PREFIX = "panic:execute"


class BotLifecycle(ABC):
    """Subclass and implement `iterate()`. Run with `await bot.run()`."""

    bot_id: str = ""  # subclass overrides
    loop_interval_seconds: int = 5  # subclass may override
    heartbeat_label: str = ""  # auto-derived from bot_id

    def __init__(self) -> None:
        if not self.bot_id:
            raise ValueError("subclass must set bot_id")
        self.heartbeat_label = f"bot:{self.bot_id}"
        self.settings = get_settings()
        self.log = get_logger(f"bot.{self.bot_id}")
        self._stop = asyncio.Event()
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._panic_task: Optional[asyncio.Task] = None
        self._redis: Optional[redis_async.Redis] = None

    # ---- subclass hooks ----------------------------------------------------

    @abstractmethod
    async def iterate(self) -> None:
        """One pass of the bot's main work. Called every `loop_interval_seconds`
        ONLY when the bot is NOT halted. This is the place for NEW-risk actions
        (opening positions / entries).

        Implementations must NOT raise — catch and log internally.
        """

    async def iterate_halted(self) -> None:
        """One pass of EXIT-ONLY work, called every `loop_interval_seconds` WHILE
        the bot IS halted. Default: no-op (preserves the old "freeze everything"
        behavior for bots that don't override).

        Override to keep RISK MANAGEMENT alive during a halt — stops, trailing
        stops, take-profit, timeout, defensive sell-signals, rug backstop — while
        still blocking new entries. A halt should stop NEW risk, not freeze the
        management of EXISTING positions (2026-06-28: a false drift halt froze
        iterate() for ~13.5h and a cluster position rode its -8% stop down to -94%
        because exits were frozen too). Must NOT open new positions; must NOT raise.
        """

    async def on_start(self) -> None:
        """Optional one-shot setup before the loop starts. Override if needed."""

    async def on_panic(self, payload: dict[str, Any]) -> None:
        """Optional venue-specific panic cleanup. Cancel orders, flatten positions.

        The fleet-level halt has already fired by the time this runs.
        """

    async def on_stop(self) -> None:
        """Optional graceful-shutdown hook. Called once on SIGTERM/SIGINT."""

    # ---- run-loop machinery ------------------------------------------------

    async def run(self) -> None:
        configure_logging()
        self.log.info("bot_starting")
        write_audit("bot_started", bot_id=self.bot_id)

        self._redis = redis_async.from_url(self.settings.redis_url, decode_responses=True)

        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._scheduler.add_job(
            self._ping_heartbeat,
            "interval",
            seconds=self.settings.heartbeat_interval_seconds,
            id="heartbeat",
        )
        self._scheduler.start()

        self._panic_task = asyncio.create_task(self._panic_listener())

        loop = asyncio.get_running_loop()
        for sig in (signal_module.SIGTERM, signal_module.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:
                pass  # Windows: signal handlers limited

        try:
            await self.on_start()
        except Exception:
            self.log.exception("on_start_failed")

        try:
            while not self._stop.is_set():
                if not is_bot_halted(self.bot_id):
                    try:
                        await self.iterate()
                    except Exception:
                        self.log.exception("iterate_failed")
                else:
                    # Halted: block new entries (skip iterate) but keep managing
                    # EXISTING positions so stops/trailing/defensive exits still fire.
                    try:
                        await self.iterate_halted()
                    except Exception:
                        self.log.exception("iterate_halted_failed")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.loop_interval_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        try:
            await self.on_stop()
        except Exception:
            self.log.exception("on_stop_failed")

        if self._panic_task:
            self._panic_task.cancel()
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
        if self._redis:
            await self._redis.aclose()
        write_audit("bot_stopped", bot_id=self.bot_id)
        self.log.info("bot_stopped")

    def _ping_heartbeat(self) -> None:
        try:
            ping(self.heartbeat_label, {"bot_id": self.bot_id})
        except Exception:
            self.log.exception("heartbeat_ping_failed")

    async def _panic_listener(self) -> None:
        if self._redis is None:
            return
        channel = f"{PANIC_CHANNEL_PREFIX}:{self.bot_id}"
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(channel)
        self.log.info("panic_listener_ready", channel=channel)

        try:
            async for msg in pubsub.listen():
                if msg["type"] != "message":
                    continue
                try:
                    payload = json.loads(msg["data"])
                except Exception:
                    payload = {"raw": msg["data"]}
                self.log.warning("panic_received", actor=payload.get("actor"))
                write_audit(
                    "bot_panic_received",
                    bot_id=self.bot_id,
                    payload=payload,
                )
                try:
                    await self.on_panic(payload)
                except Exception:
                    self.log.exception("on_panic_failed")
                # /panic is terminal — stop the loop
                self._stop.set()
        except asyncio.CancelledError:
            return
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception:
                pass
