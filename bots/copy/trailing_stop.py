"""Exit-action evaluator for COPY's tiered ladder + trailing stop.

Per brainstorm 2026-05-30 + Roy's 2026-06-08 strategy decision: removed
the +200% hard cap and replaced with a 4-tier partial-exit ladder so the
bot takes profits on the way up while keeping exposure to potential
1000x+ moonshots.

This is a pure function — no DB, no network — so unit tests don't need
the full bot surface area. Caller is responsible for firing the actions
(swap or simulated fill) and persisting the resulting state.

Key design notes:

1. **Multiplicative trailing stop.** A 25% trailing drop applies to PRICE,
   not to the percentage gain. At a peak of 4900% (50x), 25 percentage
   points off would be 4875% (a 0.5% price drop — would fire on a single
   tick of noise). 25% multiplicative is 3650% (50x → 37.5x, a real
   pullback). The old percentage-point math was strictly wrong; this is
   the corrected formula.

2. **Partials fire in tier order.** If peak gap-ups past multiple tiers
   in one cycle, ALL eligible tiers fire — caller iterates the list.
   This handles the rare but real case where price doubles between
   position-check intervals.

3. **Static stop is terminal.** If current_pct hits -EXIT_STOP_PCT, full
   close with NO partials. We never sell pieces of a loser.

4. **Trailing stop fires AFTER partials in the same cycle.** Partials
   sell defined fractions at current price; the remainder then evaluates
   trailing. So peak=300% → tier 1 fires (sells 25% at 3x), then if
   current dropped below trailing level (peak × 0.75 = 225%), full close
   fires on remainder.

Returns three things per call:
- new_peak_pct: updated peak (caller persists if higher than stored)
- partial_actions: list of (tier_pct, fraction, tier_index) that should fire
- full_close_reason: 'stop' | 'trailing_stop' | 'tp' | None
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from bots.copy.config import (
    EXIT_STOP_PCT,
    EXIT_TAKE_PROFIT_PCT,
    EXIT_TRAILING_ACTIVATION_PCT,
    EXIT_TRAILING_STOP_PCT,
    PARTIAL_EXIT_TIERS,
)


@dataclass(frozen=True)
class PartialExitAction:
    """One tier of the partial-exit ladder firing this cycle."""
    tier_pct: float       # the peak_pct threshold that triggered this
    fraction: float       # fraction of ORIGINAL position to sell (e.g. 0.25)
    tier_index: int       # 0..len(PARTIAL_EXIT_TIERS)-1


# Real memecoins can do 1000x in a great run, but not in a single
# position-check cycle, and ~never above 10,000x in a session. A ratio
# beyond this between entry_price and current_price almost certainly
# indicates a price-source bug (wrong-units math, oracle glitch, stale
# cache). Caught the 2026-06-09 cbBTC trade where pre-fix dex_quoter
# stored entry_price 10^8× too small ($0.000624 vs real $60k) and the
# partial-exit ladder cascaded all 4 tiers when Birdeye started
# returning sane current_prices again, producing $38B fake PnL.
PRICE_SCALE_ANOMALY_RATIO = 10_000.0


def is_price_scale_anomaly(
    entry_price: float,
    current_price: float,
    ratio_threshold: float = PRICE_SCALE_ANOMALY_RATIO,
) -> bool:
    """True when entry/current ratio is so extreme it's almost certainly
    a price-source bug, not a real move. Caller should skip position
    management this cycle and emit an alert.
    """
    if entry_price <= 0 or current_price <= 0:
        return False
    hi = max(entry_price, current_price)
    lo = min(entry_price, current_price)
    return (hi / lo) > ratio_threshold


def is_rug_collapse(
    entry_price: float,
    current_price: float,
    liquidity: Optional[float],
    rug_floor: float,
) -> bool:
    """When a price-scale anomaly fires, tell a GENUINE RUG apart from a
    price-FEED scale bug.

    Only the COLLAPSE direction (current < entry) with DEAD liquidity
    (< rug_floor) is a rug — the token fell toward zero and its pool is gone,
    so the position must close at ~-100% instead of getting stuck skipping
    forever (2026-07-04: two teamfollow mints rugged to 1e-10 and re-alerted
    hourly). The GAIN direction (current >> entry) is the cbBTC-style feed
    cascade the anomaly guard exists for and must stay skipped; a tiny-price
    glitch on a still-liquid pool is likewise not a rug (liquidity intact), so
    we never wrongly realize a loss on a healthy token.
    """
    if entry_price <= 0 or current_price <= 0 or liquidity is None:
        return False
    return current_price < entry_price and liquidity < rug_floor


def liquidity_aware_stop_pct(
    entry_liq: Optional[float],
    liq_smoothed: Optional[float],
    *,
    base_stop: float,
    grow_ratio: float,
    deep_stop: float,
    flat_floor: float = 0.0,
    flat_stop: Optional[float] = None,
) -> float:
    """Pick a cluster hard-stop pct from the LIVE liquidity trajectory (2026-06-28).

    - liquidity BUILDING (smoothed/entry >= grow_ratio) -> deep_stop (real token
      gaining depth; give it room to ride — the -8% stop was cutting deep-liq
      runners like ANSEM ~10x).
    - low AND flat (flat_floor>0 and smoothed < flat_floor) -> flat_stop
      (CAPABILITY, off when flat_floor=0; defaults to base_stop).
    - otherwise -> base_stop (tight rug/death stop).

    Falls back to base_stop when liquidity data is missing. Pure/testable.
    `liq_smoothed` should be an EMA of current liquidity (raw liquidity thrashes
    tick-to-tick as LPs add/remove), and grow_ratio>1 gives hysteresis so noise
    doesn't flip the stop.
    """
    if not entry_liq or entry_liq <= 0 or liq_smoothed is None or liq_smoothed <= 0:
        return base_stop
    if (liq_smoothed / entry_liq) >= grow_ratio:
        return deep_stop
    if flat_floor and flat_floor > 0 and liq_smoothed < flat_floor:
        return flat_stop if flat_stop is not None else base_stop
    return base_stop


def evaluate_exit_actions(
    *,
    entry_price: float,
    current_price: float,
    stored_peak_pct: Optional[float],
    completed_tier_indexes: tuple[int, ...] = (),
    partial_tiers: tuple[tuple[float, float], ...] = PARTIAL_EXIT_TIERS,
    leverage: float = 1.0,
    direction: str = "long",
    stop_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
    activation_pct: float = EXIT_TRAILING_ACTIVATION_PCT,
    trailing_drop_pct: float = EXIT_TRAILING_STOP_PCT,
) -> tuple[float, list[PartialExitAction], Optional[str]]:
    """Decide what exit actions to fire for a position this cycle.

    Returns (new_peak_pct, partial_actions, full_close_reason):

    - new_peak_pct: monotonic — max(stored, current_equity_pct). Caller
      persists when it ratchets up.
    - partial_actions: 0..N tier-firing actions. Caller swaps the
      configured fraction of original tokens at current price.
    - full_close_reason: if non-None, caller closes the REMAINING
      position (everything not yet partialled). Possible values:
      'stop', 'trailing_stop', 'tp'. Timeout is handled by caller
      because we don't see the entry timestamp here.

    Tier identity is the INDEX (0..N-1) into partial_tiers, NOT the
    pct value. This means re-tuning the pct thresholds is always safe
    on in-flight positions: a tier that ALREADY fired stays marked as
    completed even if its threshold changed mid-flight.

    Notes on action ordering: partials always fire first this cycle.
    If full_close is also set, it acts on whatever fraction remains
    AFTER the partials fire.
    """
    if entry_price <= 0:
        return (stored_peak_pct or 0.0, [], None)

    pct_move = (current_price - entry_price) / entry_price * 100.0
    if direction == "short":
        pct_move = -pct_move
    equity_pct = pct_move * leverage

    base_peak = stored_peak_pct if stored_peak_pct is not None else 0.0
    new_peak_pct = max(base_peak, equity_pct)

    # 1. Static stop — TERMINAL. Skip the partial check entirely; we
    # don't sell pieces of a loser. stop_pct <= 0 DISABLES the hard stop
    # (2026-06-27: conviction runs with no hard stop — relies on trailing +
    # follow-the-whale-out + rug backstop; the fast stop whipsawed it out of
    # whale-accumulation dips that recovered).
    if stop_pct is not None and stop_pct > 0 and equity_pct <= -float(stop_pct):
        return (new_peak_pct, [], "stop")

    # 2. Detect partial tiers that should fire this cycle. Identity is
    # tier index — bullet-proof against config changes mid-flight.
    partial_actions: list[PartialExitAction] = []
    completed_set = {int(i) for i in completed_tier_indexes}
    for idx, (tier_pct, fraction) in enumerate(partial_tiers):
        if idx in completed_set:
            continue
        if new_peak_pct >= tier_pct:
            partial_actions.append(PartialExitAction(
                tier_pct=tier_pct,
                fraction=fraction,
                tier_index=idx,
            ))

    # 3. Trailing stop — MULTIPLICATIVE. Convert peak_pct → peak price
    # multiplier (1 + peak/100). Trailing level multiplier = peak *
    # (1 - drop/100). Re-express as peak_pct equivalent.
    full_close: Optional[str] = None
    if new_peak_pct >= activation_pct:
        peak_mult = 1.0 + new_peak_pct / 100.0
        trail_mult = peak_mult * (1.0 - trailing_drop_pct / 100.0)
        trail_pct = (trail_mult - 1.0) * 100.0
        if equity_pct <= trail_pct:
            full_close = "trailing_stop"
    elif take_profit_pct is not None and equity_pct >= float(take_profit_pct):
        # Static TP fallback — only reachable if peak somehow skipped
        # the activation threshold. Defensive — peak is monotonic so
        # this is rare (only on oracle gaps).
        full_close = "tp"

    return (new_peak_pct, partial_actions, full_close)


# Backwards-compat shim. The old call site signature returned
# (new_peak_pct, exit_reason). New callers use evaluate_exit_actions
# directly. Kept for any caller that hasn't migrated yet.
def evaluate_trailing_exit(
    *,
    entry_price: float,
    current_price: float,
    stored_peak_pct: Optional[float],
    leverage: float = 1.0,
    direction: str = "long",
    stop_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
    activation_pct: float = EXIT_TRAILING_ACTIVATION_PCT,
    trailing_drop_pct: float = EXIT_TRAILING_STOP_PCT,
    # Old `hard_cap_pct` arg accepted but ignored — replaced by tier 4
    # of PARTIAL_EXIT_TIERS at 1000x. Kept in signature for backwards
    # compat with any caller still passing it.
    hard_cap_pct: float = 0.0,
) -> tuple[float, Optional[str]]:
    """Deprecated single-exit shim. New code should use
    evaluate_exit_actions which returns the full action list including
    partial-exit tiers.
    """
    new_peak, partials, full_close = evaluate_exit_actions(
        entry_price=entry_price,
        current_price=current_price,
        stored_peak_pct=stored_peak_pct,
        completed_tier_indexes=(),
        leverage=leverage,
        direction=direction,
        stop_pct=stop_pct,
        take_profit_pct=take_profit_pct,
        activation_pct=activation_pct,
        trailing_drop_pct=trailing_drop_pct,
    )
    # Shim semantics: if any partial would fire but the caller doesn't
    # know about partials, treat the FIRST partial as a full-close
    # signal so we don't silently leave a position open expecting
    # partial-exit machinery that doesn't exist.
    if partials and full_close is None:
        # Synthesize a 'tier_complete'-style reason for legacy callers
        return (new_peak, "trailing_stop")
    return (new_peak, full_close)
