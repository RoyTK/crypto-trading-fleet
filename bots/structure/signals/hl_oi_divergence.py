"""Open Interest Divergence signal generator.

Logic (locked v0):
- For top-15-volume assets only (matches funding_fade scope)
- Maintain 4-hour rolling buffer of (timestamp_ms, oi_native, mid_price)
- Trigger when over 4h window:
    LONG  : OI Δ ≥ +20% AND price Δ ≤ -2%
    SHORT : OI Δ ≥ +20% AND price Δ ≥ +2%
- Validation: open interest >= $10M (filter thin markets)
- Fade-the-crowd thesis: rising OI alongside a price move = late entrants
  piling into the trend. Counter-trade them as the squeeze unwinds.

State: stateful — single instance held by the bot loop. Per-asset deque of
snapshots; pruned each evaluate(). On cold start the buffer is empty and
no signals fire until ~4h of snapshots accumulate. That's expected.

Signals are debounced via _already_fired (one per asset per window).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from time import time
from typing import Optional

from bots.structure.config import (
    OI_DIV_OI_DELTA_PCT,
    OI_DIV_OI_FLOOR_USD,
    OI_DIV_PRICE_DELTA_PCT,
    OI_DIV_STOP_PCT,
    OI_DIV_TAKE_PROFIT_PCT,
    OI_DIV_TIMEOUT_HOURS,
    OI_DIV_TOP_VOL_RANK,
    OI_DIV_WINDOW_HOURS,
)
from bots.structure.signals.base import SignalCandidate
from bots.structure.venue import AssetCtx


WINDOW_SECONDS = OI_DIV_WINDOW_HOURS * 3600
WINDOW_MS = WINDOW_SECONDS * 1000


@dataclass
class _AssetWindow:
    """Per-asset rolling buffer of (timestamp_ms, oi_native, mid_price)."""
    snapshots: deque = field(default_factory=deque)


class OIDivergenceDetector:
    """Stateful detector. Single instance held by the bot loop."""

    def __init__(self) -> None:
        self._windows: dict[str, _AssetWindow] = {}
        self._already_fired: dict[str, int] = {}  # asset -> last fired ts_ms

    def observe_snapshot(
        self,
        asset: str,
        open_interest_usd: float,
        mid_price: float,
        timestamp_ms: Optional[int] = None,
    ) -> None:
        """Push a snapshot into the rolling window.

        Stores NATIVE OI (contract count) computed as oi_usd / mid_price.
        OI delta must reflect actual position-count changes, not price-driven
        re-marking. Storing oi_usd directly would conflate "price went up 10%"
        with "10% more contracts opened" — both inflate USD OI but only one
        is the signal we want.
        """
        if open_interest_usd <= 0 or mid_price <= 0:
            return
        oi_native = open_interest_usd / mid_price
        ts = timestamp_ms if timestamp_ms is not None else int(time() * 1000)
        w = self._windows.setdefault(asset, _AssetWindow())
        w.snapshots.append((ts, oi_native, mid_price))

    def _prune(self, asset: str, now_ms: int) -> None:
        """Drop snapshots older than the rolling window."""
        cutoff = now_ms - WINDOW_MS
        w = self._windows.get(asset)
        if w is None:
            return
        while w.snapshots and w.snapshots[0][0] < cutoff:
            w.snapshots.popleft()

    def _baseline(self, w: _AssetWindow, now_ms: int) -> Optional[tuple[int, float, float]]:
        """Return the oldest snapshot still within the window, or None."""
        cutoff = now_ms - WINDOW_MS
        for ts, oi, px in w.snapshots:
            if ts >= cutoff:
                return (ts, oi, px)
        return None

    def evaluate(
        self,
        asset_ctxs: list[AssetCtx],
        now_ms: Optional[int] = None,
    ) -> list[SignalCandidate]:
        if not asset_ctxs:
            return []
        ts = now_ms if now_ms is not None else int(time() * 1000)

        top_assets = {
            c.asset
            for c in sorted(asset_ctxs, key=lambda c: c.day_volume_usd, reverse=True)[
                :OI_DIV_TOP_VOL_RANK
            ]
        }
        # Map asset -> ctx for quick lookup
        ctx_by_asset = {c.asset: c for c in asset_ctxs}

        candidates: list[SignalCandidate] = []
        for asset in list(self._windows.keys()):
            self._prune(asset, ts)
            if asset not in top_assets:
                continue
            w = self._windows[asset]
            if len(w.snapshots) < 2:
                continue

            # Need at least 80% of the window covered to be statistically meaningful.
            # Otherwise a tiny baseline spike could produce huge percentage moves.
            window_age_ms = w.snapshots[-1][0] - w.snapshots[0][0]
            if window_age_ms < int(WINDOW_MS * 0.8):
                continue

            baseline = self._baseline(w, ts)
            if baseline is None:
                continue
            base_ts, base_oi, base_px = baseline
            cur_ts, cur_oi, cur_px = w.snapshots[-1]

            ctx = ctx_by_asset.get(asset)
            if ctx is None or ctx.open_interest_usd < OI_DIV_OI_FLOOR_USD:
                continue

            if base_oi <= 0 or base_px <= 0:
                continue

            oi_delta_pct = (cur_oi - base_oi) / base_oi * 100.0
            price_delta_pct = (cur_px - base_px) / base_px * 100.0

            # Both legs require OI growth above threshold
            if oi_delta_pct < OI_DIV_OI_DELTA_PCT:
                continue

            direction: Optional[str] = None
            trigger: Optional[str] = None
            if price_delta_pct <= -OI_DIV_PRICE_DELTA_PCT:
                # Price falling + OI growing → fresh shorts piling in → fade LONG
                direction = "long"
                trigger = "fresh_shorts"
            elif price_delta_pct >= OI_DIV_PRICE_DELTA_PCT:
                # Price rallying + OI growing → fresh longs piling in → fade SHORT
                direction = "short"
                trigger = "fresh_longs"
            else:
                continue

            # Don't re-fire while prior signal's window is still active
            last = self._already_fired.get(asset, 0)
            if (ts - last) < WINDOW_MS:
                continue

            # Conviction scales with how far past threshold we are (capped 1.0).
            # Use the smaller of OI-overshoot vs price-overshoot to keep
            # both legs symmetric.
            oi_overshoot = (oi_delta_pct - OI_DIV_OI_DELTA_PCT) / OI_DIV_OI_DELTA_PCT
            price_overshoot = (
                abs(price_delta_pct) - OI_DIV_PRICE_DELTA_PCT
            ) / OI_DIV_PRICE_DELTA_PCT
            conviction = max(0.4, min(1.0, min(oi_overshoot, price_overshoot)))

            candidates.append(SignalCandidate(
                signal_type="hl_oi_divergence",
                asset=asset,
                direction=direction,
                conviction=conviction,
                stop_pct=OI_DIV_STOP_PCT,
                take_profit_pct=OI_DIV_TAKE_PROFIT_PCT,
                timeout_hours=OI_DIV_TIMEOUT_HOURS,
                payload={
                    "trigger": trigger,
                    "oi_delta_pct": round(oi_delta_pct, 2),
                    "price_delta_pct": round(price_delta_pct, 2),
                    "window_hours": OI_DIV_WINDOW_HOURS,
                    "base_oi_native": base_oi,
                    "current_oi_native": cur_oi,
                    "base_price": base_px,
                    "current_price": cur_px,
                    "current_oi_usd_now": ctx.open_interest_usd,
                    "snapshot_count": len(w.snapshots),
                },
            ))
            self._already_fired[asset] = ts

        return candidates
