"""Liquidation Cascade signal generator.

Logic (locked v0):
- Subscribe to Hyperliquid liquidations websocket
- Maintain 5-min rolling per-asset notional aggregates by side (long-liqs vs short-liqs)
- Trigger when in-window notional >= $5M AND price moved >= 4% in same window
- Asset must be top-15 by day volume
- Direction: COUNTER the cascade (longs being liquidated → enter LONG)
- Sizing: 5-12% at 3x leverage; stop 4%; take-profit 3%

State: this generator is stateful — it holds an in-memory rolling buffer of
liquidation events. The bot loop pushes events via observe_liquidation()
and periodically calls evaluate() to emit candidates.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from time import time
from typing import Any

from bots.structure.config import (
    LIQ_NOTIONAL_THRESHOLD_USD,
    LIQ_PRICE_MOVE_THRESHOLD_PCT,
    LIQ_STOP_PCT,
    LIQ_TAKE_PROFIT_PCT,
    LIQ_TOP_VOL_RANK,
    LIQ_WINDOW_MINUTES,
)
from bots.structure.signals.base import SignalCandidate
from bots.structure.venue import AssetCtx, LiquidationEvent


WINDOW_SECONDS = LIQ_WINDOW_MINUTES * 60


@dataclass
class _AssetWindow:
    """In-memory rolling 5-min state per asset."""
    liqs: deque = field(default_factory=deque)        # (timestamp_ms, side, notional)
    prices: deque = field(default_factory=deque)      # (timestamp_ms, price)


class LiquidationCascadeDetector:
    """Stateful detector. Single instance held by the bot loop."""

    def __init__(self) -> None:
        self._windows: dict[str, _AssetWindow] = {}
        self._already_fired: dict[str, int] = {}  # asset -> last fired timestamp_ms

    def observe_liquidation(self, ev: LiquidationEvent) -> None:
        w = self._windows.setdefault(ev.asset, _AssetWindow())
        w.liqs.append((ev.timestamp_ms, ev.side, ev.notional_usd))
        w.prices.append((ev.timestamp_ms, ev.price))

    def observe_price(self, asset: str, price: float, timestamp_ms: int | None = None) -> None:
        ts = timestamp_ms or int(time() * 1000)
        w = self._windows.setdefault(asset, _AssetWindow())
        w.prices.append((ts, price))

    def _prune(self, asset: str, now_ms: int) -> None:
        cutoff = now_ms - WINDOW_SECONDS * 1000
        w = self._windows.get(asset)
        if w is None:
            return
        while w.liqs and w.liqs[0][0] < cutoff:
            w.liqs.popleft()
        while w.prices and w.prices[0][0] < cutoff:
            w.prices.popleft()

    def evaluate(
        self,
        asset_ctxs: list[AssetCtx],
        now_ms: int | None = None,
    ) -> list[SignalCandidate]:
        if not asset_ctxs:
            return []
        ts = now_ms or int(time() * 1000)
        now_iso = ts

        top_assets = {
            c.asset for c in sorted(asset_ctxs, key=lambda c: c.day_volume_usd, reverse=True)[:LIQ_TOP_VOL_RANK]
        }

        candidates: list[SignalCandidate] = []
        for asset in list(self._windows.keys()):
            self._prune(asset, ts)
            if asset not in top_assets:
                continue
            w = self._windows[asset]
            if not w.liqs or len(w.prices) < 2:
                continue

            # Notional aggregated by which side got liquidated
            long_liqs = sum(n for _, side, n in w.liqs if side == "long")
            short_liqs = sum(n for _, side, n in w.liqs if side == "short")
            total_liqs = long_liqs + short_liqs
            if total_liqs < LIQ_NOTIONAL_THRESHOLD_USD:
                continue

            # 5-min price move
            p_start = w.prices[0][1]
            p_end = w.prices[-1][1]
            move_pct = abs(p_end - p_start) / p_start * 100.0 if p_start else 0.0
            if move_pct < LIQ_PRICE_MOVE_THRESHOLD_PCT:
                continue

            # Don't re-fire while an active signal is still in window
            last_fired = self._already_fired.get(asset, 0)
            if (ts - last_fired) < WINDOW_SECONDS * 1000:
                continue

            # Counter the dominant liquidation side
            if long_liqs > short_liqs:
                direction = "long"  # longs got liq'd → mean-reversion buy
                dominant_notional = long_liqs
            else:
                direction = "short"
                dominant_notional = short_liqs

            conviction = min(1.0, dominant_notional / LIQ_NOTIONAL_THRESHOLD_USD - 1.0)
            conviction = max(0.5, conviction)  # always above floor

            candidates.append(SignalCandidate(
                signal_type="liquidation_cascade",
                asset=asset,
                direction=direction,
                conviction=conviction,
                stop_pct=LIQ_STOP_PCT,
                take_profit_pct=LIQ_TAKE_PROFIT_PCT,
                payload={
                    "long_liqs_usd": long_liqs,
                    "short_liqs_usd": short_liqs,
                    "total_liqs_usd": total_liqs,
                    "price_move_pct": move_pct,
                    "window_minutes": LIQ_WINDOW_MINUTES,
                    "p_start": p_start,
                    "p_end": p_end,
                },
            ))
            self._already_fired[asset] = now_iso
        return candidates
