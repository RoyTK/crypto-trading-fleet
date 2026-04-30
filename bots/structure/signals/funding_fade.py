"""Funding Fade signal generator.

Logic (locked v0):
- For top-20-volume assets only
- Compute annualized funding rate = funding_per_hour * 24 * 365
- Trigger SHORT: annualized > +50% (long pressure too high; expect mean reversion)
- Trigger LONG:  annualized < -30% (short pressure too high)
- Validation: open interest >= $10M
- Exit when annualized returns to ±10% OR 24h timeout

State: pure function over the current asset_contexts() snapshot. The bot loop
calls evaluate() on its 30s funding poll cadence; deduplication of repeat
signals against open positions is the loop's responsibility.
"""
from __future__ import annotations

from typing import Any

from bots.structure.config import (
    FUNDING_LONG_THRESHOLD_PCT,
    FUNDING_OI_FLOOR_USD,
    FUNDING_SHORT_THRESHOLD_PCT,
    FUNDING_STOP_PCT,
    FUNDING_TIMEOUT_HOURS,
    FUNDING_TOP_VOL_RANK,
)
from bots.structure.signals.base import SignalCandidate
from bots.structure.venue import AssetCtx


HOURS_PER_YEAR = 24 * 365


def annualized_pct(funding_rate_hourly: float) -> float:
    """Convert hourly funding (decimal, e.g. 0.0001) to annualized percent."""
    return funding_rate_hourly * HOURS_PER_YEAR * 100.0


def evaluate(asset_ctxs: list[AssetCtx]) -> list[SignalCandidate]:
    if not asset_ctxs:
        return []

    # Top-20 by volume
    by_vol = sorted(asset_ctxs, key=lambda c: c.day_volume_usd, reverse=True)
    top = by_vol[:FUNDING_TOP_VOL_RANK]

    candidates: list[SignalCandidate] = []
    for ctx in top:
        if ctx.open_interest_usd < FUNDING_OI_FLOOR_USD:
            continue
        ann_pct = annualized_pct(ctx.funding_rate_hourly)
        if ann_pct > FUNDING_SHORT_THRESHOLD_PCT:
            # Long pressure too high → fade short
            distance_above = ann_pct - FUNDING_SHORT_THRESHOLD_PCT
            conviction = min(1.0, distance_above / FUNDING_SHORT_THRESHOLD_PCT)
            candidates.append(SignalCandidate(
                signal_type="funding_fade",
                asset=ctx.asset,
                direction="short",
                conviction=conviction,
                stop_pct=FUNDING_STOP_PCT,
                timeout_hours=FUNDING_TIMEOUT_HOURS,
                payload={
                    "annualized_funding_pct": ann_pct,
                    "open_interest_usd": ctx.open_interest_usd,
                    "mid_price": ctx.mid_price,
                    "trigger": "above_short_threshold",
                },
            ))
        elif ann_pct < FUNDING_LONG_THRESHOLD_PCT:
            distance_below = FUNDING_LONG_THRESHOLD_PCT - ann_pct
            conviction = min(1.0, distance_below / abs(FUNDING_LONG_THRESHOLD_PCT))
            candidates.append(SignalCandidate(
                signal_type="funding_fade",
                asset=ctx.asset,
                direction="long",
                conviction=conviction,
                stop_pct=FUNDING_STOP_PCT,
                timeout_hours=FUNDING_TIMEOUT_HOURS,
                payload={
                    "annualized_funding_pct": ann_pct,
                    "open_interest_usd": ctx.open_interest_usd,
                    "mid_price": ctx.mid_price,
                    "trigger": "below_long_threshold",
                },
            ))
    return candidates
