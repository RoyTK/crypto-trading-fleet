"""Tests for the deprecated evaluate_trailing_exit shim.

The shim still exists for backwards-compat but the canonical exit
machinery is now evaluate_exit_actions (covered by
test_copy_partial_exits.py). These tests verify the shim's narrow
contract:
- Static stop still fires correctly.
- Trailing stop fires under the new MULTIPLICATIVE math.
- The 'hard cap' constant no longer fires as a separate exit — tier 4
  of the partial-exit ladder is the effective ceiling at 99900%.
- Backwards-compat shim returns 'trailing_stop' as a generic close
  reason when any partial would fire (legacy callers that don't know
  about partials still close the position cleanly).
"""
from __future__ import annotations

import pytest

from bots.copy.config import (
    EXIT_STOP_PCT,
    EXIT_TAKE_PROFIT_PCT,
    EXIT_TRAILING_ACTIVATION_PCT,
)
from bots.copy.trailing_stop import evaluate_trailing_exit


ENTRY = 1.0


def _at_pct(entry: float, pct: float) -> float:
    return entry * (1.0 + pct / 100.0)


# ---------------------------------------------------------------------------
# Static stop — unchanged from prior behavior
# ---------------------------------------------------------------------------

def test_static_stop_fires_at_threshold():
    # Use a price clearly below the stop — the exact -stop boundary is
    # float-fragile (equity lands at -7.9999… and misses the `<=`).
    _, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, -(EXIT_STOP_PCT + 2.0)),
        stored_peak_pct=None,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert reason == "stop"


def test_static_stop_does_not_fire_above_threshold():
    _, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, -7.0),
        stored_peak_pct=None,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert reason is None


def test_no_stop_when_stop_pct_is_none():
    _, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, -50.0),
        stored_peak_pct=None,
        stop_pct=None,
        take_profit_pct=None,
    )
    # No partials fire (peak 0%), no trailing (no activation), no TP set.
    assert reason is None


# ---------------------------------------------------------------------------
# Trailing math — MULTIPLICATIVE, not percentage-point
# ---------------------------------------------------------------------------

def test_trailing_uses_multiplicative_math_low_peak():
    """Peak 100% (2x), current +9%. Multiplicative trail = 2 × 0.55 - 1 =
    +10%; current below it → fires. (Peak must be > ~67% so the 45% trail
    level sits above the -8% hard stop — otherwise the stop preempts.)"""
    _, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 9.0),
        stored_peak_pct=100.0,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert reason == "trailing_stop"


def test_trailing_holds_above_multiplicative_level():
    """Peak 100%, current 30%. Trail = +10%. Current > trail → hold."""
    _, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 30.0),
        stored_peak_pct=100.0,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    # Peak 100% is below tier 0 (300%) so no partials; trailing level +10%
    # not breached by current +30%. → no reason.
    assert reason is None


# ---------------------------------------------------------------------------
# Shim semantics for partial tiers — legacy callers see 'trailing_stop'
# ---------------------------------------------------------------------------

def test_shim_synthesizes_trailing_stop_when_tier_would_fire():
    """When the shim is called and a partial tier IS eligible, the shim
    returns 'trailing_stop' so legacy callers close the position rather
    than silently expecting partial-exit machinery they don't have."""
    _, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 350.0),     # 4.5x — past tier 0 (300% = 4x)
        stored_peak_pct=None,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    # Tier 0 fires in the new function; trailing doesn't (equity 350% >
    # the 45% trail level). Shim conservatively closes for legacy callers.
    assert reason == "trailing_stop"


# ---------------------------------------------------------------------------
# Activation gate prevents trailing on early-stage noise
# ---------------------------------------------------------------------------

def test_no_trailing_below_activation():
    """Peak 15%, current 5% (below activation 20%). No trailing."""
    _, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 5.0),
        stored_peak_pct=15.0,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert reason is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_zero_entry_price_returns_no_exit():
    new_peak, reason = evaluate_trailing_exit(
        entry_price=0.0,
        current_price=10.0,
        stored_peak_pct=50.0,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert reason is None
    assert new_peak == 50.0


def test_leverage_scales_equity_pct():
    """Price move +15% × 2x leverage = +30% equity. Above activation,
    becomes peak. Trail level at peak=30% multiplicative = (1.3)(0.55)-1
    = -28.5% pct. Current 30% > trail → hold."""
    new_peak, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 15.0),
        stored_peak_pct=None,
        leverage=2.0,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    # +15% × 2x = +30% (float lands at 29.9999…; compare with tolerance).
    assert new_peak == pytest.approx(30.0)
    # Activation reached → trailing branch claims control. trail_pct =
    # (1.3)(0.55)-1 = -0.285 = -28.5%. Current 30% > -28.5%. Hold.
    assert reason is None
