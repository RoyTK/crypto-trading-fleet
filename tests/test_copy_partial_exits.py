"""Pure-function tests for the partial-exit ladder + multiplicative trailing
stop. No DB, no network — only the evaluator decision logic.

Behaviors under test:

- Static stop terminates everything (no partials on a loser)
- Tier 1 fires when peak crosses 200%, only once
- Multiple tiers fire in one cycle when peak gap-ups past several
- Tier 4 firing means the position fully exits via partials (caller
  closes with 'tier_complete')
- Trailing stop is MULTIPLICATIVE — 25% drop from peak in PRICE terms
- Trailing fires AFTER partials in the same cycle
- Already-completed tiers don't re-fire
- TP fallback only when peak skips activation (defensive)
"""
from __future__ import annotations

from bots.copy.config import (
    EXIT_STOP_PCT,
    EXIT_TAKE_PROFIT_PCT,
    EXIT_TRAILING_ACTIVATION_PCT,
    PARTIAL_EXIT_TIERS,
)
from bots.copy.trailing_stop import (
    PartialExitAction,
    evaluate_exit_actions,
)


ENTRY = 1.0


def _at_pct(entry: float, pct: float) -> float:
    """Helper: return the price corresponding to entry × (1 + pct/100)."""
    return entry * (1.0 + pct / 100.0)


# ---------------------------------------------------------------------------
# Static stop — terminal, no partials
# ---------------------------------------------------------------------------

def test_static_stop_terminates_with_no_partials():
    """Even if peak previously crossed tier 1, a stop-out closes
    everything (no piecewise sells of a loser)."""
    new_peak, partials, full = evaluate_exit_actions(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, -EXIT_STOP_PCT),
        stored_peak_pct=300.0,    # had been at 4x peak previously
        completed_tier_pcts=(200.0,),    # tier 1 already fired
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert full == "stop"
    assert partials == []   # don't propose new partials on a stop-out


# ---------------------------------------------------------------------------
# Tier firing — basic
# ---------------------------------------------------------------------------

def test_tier_1_fires_at_3x_peak():
    """Pump to 3x (peak_pct = 200) → tier 1 fires once."""
    new_peak, partials, full = evaluate_exit_actions(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 200.0),
        stored_peak_pct=None,
        completed_tier_pcts=(),
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    # new_peak = 200%. Tier 1 (200) eligible. Trailing on remainder: peak
    # 200% → trail level = (1+200/100)*0.75 - 1 = 125%. Current 200% > 125%
    # → hold remainder.
    assert new_peak == 200.0
    assert len(partials) == 1
    assert partials[0].tier_pct == 200.0
    assert partials[0].fraction == 0.25
    assert partials[0].tier_index == 0
    assert full is None


def test_tier_1_does_not_refire_when_already_completed():
    """If tier 1 previously fired, it doesn't fire again."""
    new_peak, partials, full = evaluate_exit_actions(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 350.0),
        stored_peak_pct=400.0,
        completed_tier_pcts=(200.0,),    # tier 1 already done
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    # Only tiers >= peak that aren't already completed. Tier 2 (900) needs
    # peak >= 900. Peak is 400. So no new tiers.
    assert partials == []
    assert full is None    # current 350 > trailing level (400 → 275% trail) → hold


# ---------------------------------------------------------------------------
# Gap-up: multiple tiers fire in one cycle
# ---------------------------------------------------------------------------

def test_gap_up_fires_multiple_tiers_in_one_cycle():
    """Peak skips from 0 to 5000% in one cycle (memecoin gap-up).
    Tiers 1 (200%), 2 (900%), and 3 (4900%) all fire. Tier 4 (99900%) doesn't."""
    new_peak, partials, full = evaluate_exit_actions(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 5000.0),
        stored_peak_pct=None,
        completed_tier_pcts=(),
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert new_peak == 5000.0
    fired_tiers = sorted(p.tier_pct for p in partials)
    assert fired_tiers == [200.0, 900.0, 4900.0]
    # 25% of position remains. Peak 5000% → trail level = 51 × 0.75 - 1 = 37.25
    # → trail_pct = 3725%. Current 5000% > 3725% → hold remainder.
    assert full is None


def test_gap_up_through_tier_4_completes_position():
    """Peak crosses tier 4 (99900%) → all 4 tiers fire in one cycle.
    Caller is responsible for detecting 'all tiers complete' → close row."""
    new_peak, partials, full = evaluate_exit_actions(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 100_000.0),    # 1001x — past tier 4
        stored_peak_pct=None,
        completed_tier_pcts=(),
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    fired_tiers = sorted(p.tier_pct for p in partials)
    assert fired_tiers == [200.0, 900.0, 4900.0, 99900.0]
    # Note: this function doesn't return 'tier_complete' as a full_close
    # reason — the caller in main.py infers that from
    # len(completed)+len(partials) >= len(PARTIAL_EXIT_TIERS).
    assert full is None


# ---------------------------------------------------------------------------
# Multiplicative trailing — the corrected math
# ---------------------------------------------------------------------------

def test_trailing_uses_multiplicative_math_at_low_peak():
    """At peak 100%, multiplicative trail = (1+1)*0.75 - 1 = 0.5 = 50% pct.
    Old percentage-point math would have given 100 - 25 = 75% pct.
    The multiplicative version is the trader-intuition correct one."""
    new_peak, partials, full = evaluate_exit_actions(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 50.0),    # back to +50%
        stored_peak_pct=100.0,    # peak was at 2x
        completed_tier_pcts=(),
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    # equity = 50, trail level = 50. Current AT level → fires (<= comparison).
    assert full == "trailing_stop"


def test_trailing_uses_multiplicative_math_at_high_peak():
    """At peak 4900% (50x), trailing fires at 3650% (37.5x = 25% multiplicative drop).
    Old pct-point math would have fired at 4875% (0.5% price drop — noise)."""
    new_peak, partials, full = evaluate_exit_actions(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 3650.0),    # 37.5x — exactly the new trail level
        stored_peak_pct=4900.0,
        completed_tier_pcts=(200.0, 900.0, 4900.0),    # all 3 lower tiers fired
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert full == "trailing_stop"
    assert partials == []   # no new tiers (peak < 99900 = tier 4)


def test_trailing_holds_when_above_multiplicative_stop():
    """Peak 4900%, current 4000% (still 41x — well above the 37.5x trail). Hold."""
    new_peak, partials, full = evaluate_exit_actions(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 4000.0),
        stored_peak_pct=4900.0,
        completed_tier_pcts=(200.0, 900.0, 4900.0),
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert full is None
    assert partials == []


# ---------------------------------------------------------------------------
# Partial AND trailing fire in same cycle
# ---------------------------------------------------------------------------

def test_partial_and_trailing_can_fire_together():
    """Peak ratchets to 300% (4x), current dips to 200% (3x).
    Tier 1 (200%) eligible because peak crossed it. Trailing also fires
    on the remainder because trail level = (1+3)*0.75 - 1 = 200% and
    current = 200% (≤ trail level)."""
    new_peak, partials, full = evaluate_exit_actions(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 200.0),
        stored_peak_pct=300.0,
        completed_tier_pcts=(),
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert new_peak == 300.0
    assert len(partials) == 1
    assert partials[0].tier_pct == 200.0
    assert full == "trailing_stop"


# ---------------------------------------------------------------------------
# Peak monotonicity
# ---------------------------------------------------------------------------

def test_peak_is_monotonic_under_pullback():
    """Peak doesn't decrease when current pulls back."""
    new_peak, _, _ = evaluate_exit_actions(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 100.0),
        stored_peak_pct=500.0,
        completed_tier_pcts=(200.0,),
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert new_peak == 500.0


# ---------------------------------------------------------------------------
# Edge: zero entry price
# ---------------------------------------------------------------------------

def test_zero_entry_price_returns_no_actions():
    """Zero entry price defensive return — no partials, no exit."""
    new_peak, partials, full = evaluate_exit_actions(
        entry_price=0.0,
        current_price=1.0,
        stored_peak_pct=300.0,
        completed_tier_pcts=(),
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert new_peak == 300.0
    assert partials == []
    assert full is None


# ---------------------------------------------------------------------------
# Config sanity
# ---------------------------------------------------------------------------

def test_partial_tiers_are_strictly_increasing():
    """The ladder must be ordered low-to-high or the gap-up logic
    misbehaves (a tier_pct already met might be checked AFTER a smaller
    one and incorrectly considered 'already complete')."""
    pcts = [t[0] for t in PARTIAL_EXIT_TIERS]
    assert pcts == sorted(pcts)
    assert pcts == [200.0, 900.0, 4900.0, 99900.0]


def test_partial_tier_fractions_sum_to_one():
    """The 4 tiers must fully exit the position when they all fire."""
    total = sum(t[1] for t in PARTIAL_EXIT_TIERS)
    assert abs(total - 1.0) < 1e-9
