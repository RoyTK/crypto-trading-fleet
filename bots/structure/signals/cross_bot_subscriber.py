"""Cross-bot signal subscriber: COPY macro clusters → STRUCTURE observation log.

This is a pure research module (Roy + engineer 2026-05-25). It DOES NOT
generate SignalCandidates or affect STRUCTURE's paper trading. It
subscribes to copy:macro_cluster on Redis, receives cluster events from
COPY (SOL/BTC/ETH only — see MACRO_MINT_TO_HL_ASSET in bots/copy/main.py),
and writes rows to cross_bot_signal_log for outcome evaluation by the
outcome cron (framework/cross_bot_outcome_cron.py).

Wired into StructureBot.on_start() as a background task.

If 7-day forward data shows 4h directional accuracy > 55% with N >= 15
on at least one of SOL/BTC/ETH, this bridge graduates to a real
STRUCTURE signal type (resets the kill-criteria window when it does).
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

import redis.asyncio as redis_async

from framework.db import session_scope
from framework.logging_setup import get_logger
from framework.models import CrossBotSignalLog


REDIS_MACRO_CLUSTER_CHANNEL = "copy:macro_cluster"

log = get_logger(__name__)


class CrossBotSubscriber:
    """Long-running Redis subscriber. One instance held by StructureBot."""

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            client: Optional[redis_async.Redis] = None
            try:
                client = redis_async.from_url(self._redis_url, decode_responses=True)
                pubsub = client.pubsub()
                await pubsub.subscribe(REDIS_MACRO_CLUSTER_CHANNEL)
                log.info("cross_bot_subscriber_ready", channel=REDIS_MACRO_CLUSTER_CHANNEL)
                backoff = 1.0
                async for msg in pubsub.listen():
                    if msg.get("type") != "message":
                        continue
                    try:
                        data = json.loads(msg["data"])
                        await asyncio.to_thread(_write_row, data)
                    except Exception:
                        log.exception("cross_bot_event_parse_failed")
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("cross_bot_subscriber_failed", backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            finally:
                if client:
                    try:
                        await client.aclose()
                    except Exception:
                        pass


def _write_row(data: dict) -> None:
    """Synchronous DB write. Called from the subscriber loop via asyncio.to_thread."""
    try:
        with session_scope() as s:
            row = CrossBotSignalLog(
                cluster_id=data.get("cluster_id", ""),
                hl_asset=data["asset"],
                direction=data["direction"],
                wallet_count=int(data["wallet_count"]),
                cluster_size_usd=float(data.get("cluster_size_usd", 0.0)),
                event_timestamp_ms=int(data["timestamp_ms"]),
            )
            s.add(row)
        log.info(
            "cross_bot_event_persisted",
            asset=data.get("asset"),
            direction=data.get("direction"),
            cluster_id=data.get("cluster_id"),
        )
    except Exception:
        # Likely uniqueness violation on cluster_id (duplicate replay) — log + drop.
        log.warning("cross_bot_event_persist_failed", cluster_id=data.get("cluster_id"))
