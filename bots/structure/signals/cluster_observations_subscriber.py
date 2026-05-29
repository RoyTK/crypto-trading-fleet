"""Read-only journal: COPY all-clusters → STRUCTURE cluster_observations table.

Per adversarial team meeting 2026-05-28 (Optimist + Engineer R2 convergence):
COPY publishes EVERY cluster event (not just SOL/BTC/ETH) to Redis channel
`copy:all_clusters`. STRUCTURE journals these to `cluster_observations`
for future research — does NOT use them for trading decisions.

This is pure observation. Per the kill-criteria reset rules (signed
2026-05-25): logging/monitoring/observation does NOT reset STRUCTURE's
60-day kill-criteria window. Any future code that USES this data for
trading decisions WOULD reset the window — that's a separate ship.

Wired into StructureBot.on_start() as a background task. Self-restarts on
errors with exponential backoff. Drops duplicate cluster_uuid silently.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

import redis.asyncio as redis_async

from framework.db import session_scope
from framework.logging_setup import get_logger
from framework.models import ClusterObservation


REDIS_ALL_CLUSTERS_CHANNEL = "copy:all_clusters"

log = get_logger(__name__)


class ClusterObservationsSubscriber:
    """Long-running Redis subscriber. One instance per StructureBot."""

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
                await pubsub.subscribe(REDIS_ALL_CLUSTERS_CHANNEL)
                log.info("cluster_observations_subscriber_ready",
                         channel=REDIS_ALL_CLUSTERS_CHANNEL)
                backoff = 1.0
                async for msg in pubsub.listen():
                    if msg.get("type") != "message":
                        continue
                    try:
                        data = json.loads(msg["data"])
                        await asyncio.to_thread(_write_observation, data)
                    except Exception:
                        log.exception("cluster_observation_parse_failed")
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("cluster_observations_subscriber_failed", backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            finally:
                if client:
                    try:
                        await client.aclose()
                    except Exception:
                        pass


def _write_observation(data: dict) -> None:
    """Synchronous DB insert. Called via asyncio.to_thread."""
    try:
        ts_ms = int(data["timestamp_ms"])
        from datetime import datetime, timezone
        observed_at = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        with session_scope() as s:
            obs = ClusterObservation(
                cluster_uuid=data["cluster_uuid"],
                observed_at=observed_at,
                token_mint=data["token_mint"],
                hl_asset_if_any=data.get("hl_asset_if_any"),
                cluster_size=int(data["cluster_size"]),
                cluster_total_notional_usd=float(data.get("cluster_size_usd", 0.0)),
                wallet_tier=str(data.get("wallet_tier", "unknown")),
            )
            s.add(obs)
    except Exception:
        # Likely a duplicate cluster_uuid (cluster.py suppresses re-fires
        # within 15 min so unique constraint should hold, but be safe).
        log.warning("cluster_observation_persist_skipped",
                    cluster_uuid=data.get("cluster_uuid"))
