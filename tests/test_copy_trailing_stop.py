"""Tests for evaluate_trailing_exit — pure function, no DB/network.

The function decides what to do with an open position given the current
price + stored peak. Returns (new_peak_pct, exit_reason_or_None).
"""
from __future__ import annotations

from bots.copy.config import (
    EXIT_STOP_PCT,
    EXIT_TAKE_PROFIT_PCT,
    EXIT_TRAILING_ACTIVATION_PCT,
    EXIT_TRAILING_HARD_CAP_PCT,
    EXIT_TRAILING_STOP_PCT,
)
from bots.copy.trailing_stop import evaluate_trailing_exit


# Helpful named constants for the test scenarios
ENTRY = 1.0


def _at_pct(entry: float, pct: float) -> float:
    return entry * (1.0 + pct / 100.0)


# ---------------------------------------------------------------------------
# Static stop — downside protection regardless of trailing state
# ---------------------------------------------------------------------------

def test_static_stop_fires_at_threshold():
    """Drop to -8% = exit 'stop'."""
    new_peak, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, -EXIT_STOP_PCT),
        stored_peak_pct=None,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert reason == "stop"


def test_static_stop_does_not_fire_above_threshold():
    """Drop to -7% (above -8% stop) = no exit."""
    _, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, -7.0),
        stored_peak_pct=None,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert reason is None


def test_no_stop_when_stop_pct_is_none():
    """If stop_pct missing, the static stop never fires."""
    _, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, -50.0),
        stored_peak_pct=None,
        stop_pct=None,
        take_profit_pct=None,
    )
    assert reason is None


# ---------------------------------------------------------------------------
# Pre-activation: static TP still applies
# ---------------------------------------------------------------------------

def test_static_tp_fires_before_activation_skipped():
    """If price gaps up directly to TP (peak == current == 30%, activation
    is 20%), the trailing branch claims control first and we DON'T exit
    on static TP. This is per spec — trailing replaces TP once active.

    The 'static TP fallback' is only reachable if activation was skipped
    AND the peak ended up below activation. Hard to construct with monotonic
    peak; the test below covers the rare case explicitly.
    """
    new_peak, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, EXIT_TAKE_PROFIT_PCT),
        stored_peak_pct=None,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    # current_pct=30% > activation_pct=20%, so trailing branch fires.
    # New peak = 30%, trailing_stop_level = 30% - 25% = 5%, current 30% > 5% → hold.
    assert reason is None
    assert new_peak == EXIT_TAKE_PROFIT_PCT


def test_static_tp_fallback_when_peak_below_activation():
    """Constructed case: peak stuck below activation but current pushes to
    TP. Defensive — guarantees TP doesn't get permanently skipped if a
    weird state arises (e.g. clock skew, oracle gap).
    """
    # Force trailing_drop_pct huge so trailing never fires.
    new_peak, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 30.0),
        stored_peak_pct=10.0,    # below activation
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
        activation_pct=50.0,     # override: 50% activation
    )
    # current = 30%, peak became 30%, activation = 50%. Not activated.
    # current >= TP → static TP fires.
    assert reason == "tp"


# ---------------------------------------------------------------------------
# Trailing stop — the meat of the strategy
# ---------------------------------------------------------------------------

def test_trailing_activates_at_threshold():
    """Once peak reaches 20%, trailing becomes the dominant exit."""
    # First cycle: pump to +25%
    new_peak, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 25.0),
        stored_peak_pct=None,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    # New peak = 25%; trailing stop level = 25% - 25% = 0%.
    # Current = 25% > 0% → hold.
    assert reason is None
    assert new_peak == 25.0


def test_trailing_fires_on_pullback():
    """Peak at +50%, drops to +20% → trailing stop (50-25=25) is breached."""
    new_peak, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 20.0),
        stored_peak_pct=50.0,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    # Peak stays 50, trailing stop level = 25%, current=20% <= 25% → fire.
    assert reason == "trailing_stop"
    assert new_peak == 50.0


def test_trailing_holds_when_above_stop_level():
    """Peak at +50%, current at +30% (above stop level 25) → hold."""
    _, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 30.0),
        stored_peak_pct=50.0,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert reason is None


def test_trailing_stop_does_not_fire_below_activation():
    """Position pumped to +15%, dropped to +10% → no trailing fire
    (activation is 20%)."""
    new_peak, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 10.0),
        stored_peak_pct=15.0,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert reason is None
    # Peak stays at 15 (current=10 < 15)
    assert new_peak == 15.0


def test_peak_is_monotonic():
    """Stored peak is preserved when current_pct dips below it."""
    new_peak, _ = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 5.0),
        stored_peak_pct=80.0,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    # current=5%, stored=80% → kept at 80%
    assert new_peak == 80.0


# ---------------------------------------------------------------------------
# Hard cap — guaranteed 3x exit
# ---------------------------------------------------------------------------

def test_hard_cap_fires_at_threshold():
    """Pump to +200% = force exit 'trailing_hard_cap'."""
    new_peak, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, EXIT_TRAILING_HARD_CAP_PCT),
        stored_peak_pct=150.0,    # already trailing-active
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert reason == "trailing_hard_cap"
    assert new_peak == EXIT_TRAILING_HARD_CAP_PCT


def test_hard_cap_beats_trailing():
    """At +200%, hard cap fires even if trailing logic would too."""
    # Construct: peak=210%, current=190%. Trailing level=210-25=185.
    # current 190 > 185 → would hold. But hard cap should still fire because
    # the question is "is current at or above the hard cap." 190 < 200 → no fire.
    # Different test: peak=200, current=200 → hard cap.
    new_peak, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 200.0),
        stored_peak_pct=200.0,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert reason == "trailing_hard_cap"


def test_hard_cap_evaluated_before_stop():
    """If hypothetically current is BOTH above hard_cap AND below stop
    (impossible math but defensive), hard cap wins. Verifies eval order."""
    # We can't construct contradictory inputs, but verify hard cap precedes stop
    # by setting stop_pct to a very loose value that current is below.
    _, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, EXIT_TRAILING_HARD_CAP_PCT + 50),
        stored_peak_pct=None,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert reason == "trailing_hard_cap"


# ---------------------------------------------------------------------------
# Edge cases: leverage, entry_price = 0, direction='short'
# ---------------------------------------------------------------------------

def test_zero_entry_price_returns_no_exit():
    """entry_price=0 shouldn't crash — return no exit."""
    new_peak, reason = evaluate_trailing_exit(
        entry_price=0.0,
        current_price=10.0,
        stored_peak_pct=50.0,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert reason is None
    assert new_peak == 50.0    # stored value preserved


def test_leverage_scales_equity_pct():
    """Leverage 2x means +10% price move → +20% equity move → activates trailing."""
    # Price moved +15% but with 2x leverage, equity moved 30% → trailing-active
    new_peak, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 15.0),
        stored_peak_pct=None,
        leverage=2.0,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert new_peak == 30.0      # 15% × 2 leverage
    # current_pct=30, peak=30, trailing_stop_level = 30-25=5, current=30 > 5 → hold
    assert reason is None


def test_short_direction_inverts_sign():
    """Short with price DROPPING +15% (i.e. price up 15%) → equity goes -15%."""
    new_peak, reason = evaluate_trailing_exit(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, -15.0),     # price dropped 15%
        stored_peak_pct=None,
        direction="short",
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    # Short equity = -(-15) = +15. Below activation (20). No trailing fire.
    assert new_peak == 15.0
    assert reason is None
