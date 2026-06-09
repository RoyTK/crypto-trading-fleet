"""Pure-function tests for the partial-exit ladder + multiplicative trailing
stop. No DB, no network — only the evaluator decision logic.

Behaviors under test:

- Static stop terminates everything (no partials on a loser)
- Tier 1 fires when peak crosses 300% (4x), only once
- Multiple tiers fire in one cycle when peak gap-ups past several
- Tier 4 firing means the position fully exits via partials
- Trailing stop is MULTIPLICATIVE — 25% drop from peak in PRICE terms
- Trailing fires AFTER partials in the same cycle
- Already-completed tiers (tracked by INDEX, not pct) don't re-fire
- TP fallback only when peak skips activation (defensive)
- Env-driven tier ladder parses safely and falls back on bad input
- Identity is tier_index — re-tuning tier_pct mid-flight is safe
"""
from __future__ import annotations

from bots.copy.config import (
    EXIT_STOP_PCT,
    EXIT_TAKE_PROFIT_PCT,
    EXIT_TRAILING_ACTIVATION_PCT,
    PARTIAL_EXIT_TIERS,
    CopySettings,
)
from bots.copy.trailing_stop import (
    PARTIAL_EXIT_TIERS as TRAIL_DEFAULT_TIERS,
    PRICE_SCALE_ANOMALY_RATIO,
    PartialExitAction,
    evaluate_exit_actions,
    is_price_scale_anomaly,
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
        stored_peak_pct=400.0,    # had been at 5x peak previously
        completed_tier_indexes=(0,),    # tier 0 (4x) already fired
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert full == "stop"
    assert partials == []   # don't propose new partials on a stop-out


# ---------------------------------------------------------------------------
# Tier firing — basic
# ---------------------------------------------------------------------------

def test_tier_0_fires_at_4x_peak():
    """Pump to 4x (peak_pct = 300) → tier 0 fires once. (Default ladder
    has tier 0 at 300% = 4x.)"""
    new_peak, partials, full = evaluate_exit_actions(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 300.0),
        stored_peak_pct=None,
        completed_tier_indexes=(),
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    # new_peak = 300%. Tier 0 (300) eligible. Trailing on remainder: peak
    # 300% → trail mult = (1+300/100)*0.75 = 3. trail_pct = 200%. Current
    # 300% > 200% → hold remainder.
    assert new_peak == 300.0
    assert len(partials) == 1
    assert partials[0].tier_pct == 300.0
    assert partials[0].fraction == 0.25
    assert partials[0].tier_index == 0
    assert full is None


def test_tier_0_does_not_refire_when_already_completed():
    """If tier 0 previously fired, it doesn't fire again — identity is
    tier_index 0, regardless of pct value."""
    new_peak, partials, full = evaluate_exit_actions(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 500.0),
        stored_peak_pct=500.0,
        completed_tier_indexes=(0,),    # tier 0 already done
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    # Tier 1 (900) needs peak >= 900. Peak 500 < 900 → no fire.
    assert partials == []
    # Trailing: peak 500 → trail mult 6 × 0.75 = 4.5 → trail_pct 350%.
    # Current 500 > 350 → hold.
    assert full is None


def test_tier_identity_is_index_not_pct():
    """If the config has been re-tuned mid-flight (tier 0 was 200, now
    300), a position that already fired the OLD tier 0 at 200% stays
    marked complete — won't double-fire on the new tier 0 at 300%."""
    new_peak, partials, full = evaluate_exit_actions(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 400.0),
        stored_peak_pct=400.0,
        completed_tier_indexes=(0,),    # tier 0 was fired (at whatever pct)
        partial_tiers=((300.0, 0.25), (900.0, 0.25)),    # current config
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    # Tier 0 (300) is in completed_set as INDEX 0, not as pct=300. So even
    # though peak 400 >= 300, tier 0 doesn't refire.
    assert all(p.tier_index != 0 for p in partials)


# ---------------------------------------------------------------------------
# Gap-up: multiple tiers fire in one cycle
# ---------------------------------------------------------------------------

def test_gap_up_fires_multiple_tiers_in_one_cycle():
    """Peak skips from 0 to 5000% in one cycle (memecoin gap-up).
    Tiers 0 (300%), 1 (900%), 2 (4900%) all fire. Tier 3 (99900%) doesn't."""
    new_peak, partials, full = evaluate_exit_actions(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 5000.0),
        stored_peak_pct=None,
        completed_tier_indexes=(),
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert new_peak == 5000.0
    fired_indexes = sorted(p.tier_index for p in partials)
    assert fired_indexes == [0, 1, 2]
    # Trailing on remainder: peak 5000 → trail mult 51 × 0.75 = 38.25 →
    # trail_pct 3725%. Current 5000 > 3725 → hold.
    assert full is None


def test_gap_up_through_tier_3_fires_all_four():
    """Peak crosses tier 3 (99900%) → all 4 tiers fire in one cycle.
    Caller infers 'tier_complete' from len(completed)+len(this_cycle) ==
    len(tiers)."""
    new_peak, partials, full = evaluate_exit_actions(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 100_000.0),    # past tier 3
        stored_peak_pct=None,
        completed_tier_indexes=(),
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    fired_indexes = sorted(p.tier_index for p in partials)
    assert fired_indexes == [0, 1, 2, 3]
    assert full is None


# ---------------------------------------------------------------------------
# Multiplicative trailing — the corrected math
# ---------------------------------------------------------------------------

def test_trailing_uses_multiplicative_math_at_high_peak():
    """At peak 4900% (50x), trailing fires at 3650% (37.5x = 25% multiplicative drop).
    Old pct-point math would have fired at 4875% (0.5% price drop — noise)."""
    new_peak, partials, full = evaluate_exit_actions(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 3650.0),    # exactly trail level
        stored_peak_pct=4900.0,
        completed_tier_indexes=(0, 1, 2),    # all lower tiers fired
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert full == "trailing_stop"
    assert partials == []   # no new tiers (peak < 99900 = tier 3)


def test_trailing_holds_above_multiplicative_stop():
    """Peak 4900%, current 4000% (41x — well above the 37.5x trail). Hold."""
    new_peak, partials, full = evaluate_exit_actions(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 4000.0),
        stored_peak_pct=4900.0,
        completed_tier_indexes=(0, 1, 2),
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert full is None
    assert partials == []


# ---------------------------------------------------------------------------
# Partial AND trailing fire in same cycle
# ---------------------------------------------------------------------------

def test_partial_and_trailing_can_fire_together():
    """Peak ratchets to 400% (5x), current dips to 275%.
    Tier 0 (300%) eligible because peak crossed it. Trailing also fires
    on the remainder because trail mult = 5 × 0.75 = 3.75 → trail_pct
    275% and current = 275% (at trail level)."""
    new_peak, partials, full = evaluate_exit_actions(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 275.0),
        stored_peak_pct=400.0,
        completed_tier_indexes=(),
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert new_peak == 400.0
    assert len(partials) == 1
    assert partials[0].tier_index == 0
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
        completed_tier_indexes=(0,),
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert new_peak == 500.0


# ---------------------------------------------------------------------------
# Edge: zero entry price
# ---------------------------------------------------------------------------

def test_zero_entry_price_returns_no_actions():
    new_peak, partials, full = evaluate_exit_actions(
        entry_price=0.0,
        current_price=1.0,
        stored_peak_pct=300.0,
        completed_tier_indexes=(),
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert new_peak == 300.0
    assert partials == []
    assert full is None


# ---------------------------------------------------------------------------
# Config sanity (canonical default)
# ---------------------------------------------------------------------------

def test_default_partial_tiers_are_strictly_increasing():
    """The ladder must be ordered low-to-high or the gap-up logic
    misbehaves."""
    pcts = [t[0] for t in PARTIAL_EXIT_TIERS]
    assert pcts == sorted(pcts)
    assert pcts == [300.0, 900.0, 4900.0, 99900.0]


def test_default_partial_tier_fractions_sum_to_one():
    """The 4 default tiers must fully exit the position when they all
    fire. Custom env configs may set fractions that don't sum to 1
    (long-term rider design); the default does."""
    total = sum(t[1] for t in PARTIAL_EXIT_TIERS)
    assert abs(total - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# Env-driven tier parsing
# ---------------------------------------------------------------------------

def test_env_parses_default_format():
    s = CopySettings(copy_partial_exit_tiers="300:0.25,900:0.25,4900:0.25,99900:0.25")
    assert s.get_partial_exit_tiers() == (
        (300.0, 0.25), (900.0, 0.25), (4900.0, 0.25), (99900.0, 0.25),
    )


def test_env_parses_with_whitespace():
    s = CopySettings(copy_partial_exit_tiers=" 300 : 0.25 , 900 : 0.25 ")
    assert s.get_partial_exit_tiers() == ((300.0, 0.25), (900.0, 0.25))


def test_env_sorts_unordered_tiers():
    s = CopySettings(copy_partial_exit_tiers="900:0.25,300:0.25,4900:0.5")
    assert s.get_partial_exit_tiers() == (
        (300.0, 0.25), (900.0, 0.25), (4900.0, 0.5),
    )


def test_env_falls_back_on_empty():
    s = CopySettings(copy_partial_exit_tiers="")
    assert s.get_partial_exit_tiers() == PARTIAL_EXIT_TIERS


def test_env_falls_back_on_malformed():
    """A bad value in the middle invalidates the whole list, fallback
    keeps the bot running."""
    s = CopySettings(copy_partial_exit_tiers="300:0.25,not-a-number")
    assert s.get_partial_exit_tiers() == PARTIAL_EXIT_TIERS


def test_env_rejects_invalid_fraction():
    """fraction > 1.0 is rejected as a config bug → fallback."""
    s = CopySettings(copy_partial_exit_tiers="300:1.5")
    assert s.get_partial_exit_tiers() == PARTIAL_EXIT_TIERS


def test_env_accepts_long_term_rider_config():
    """User wants 4 tiers × 20% = leave 20% riding forever. Valid."""
    s = CopySettings(copy_partial_exit_tiers="300:0.2,900:0.2,4900:0.2,99900:0.2")
    tiers = s.get_partial_exit_tiers()
    total = sum(t[1] for t in tiers)
    assert abs(total - 0.8) < 1e-9    # 20% rider remaining


def test_custom_ladder_fires_at_custom_thresholds():
    """End-to-end: a non-default ladder via env produces the right
    fire decisions."""
    custom = ((100.0, 0.5), (500.0, 0.5))    # 2x and 6x, 50% each
    new_peak, partials, full = evaluate_exit_actions(
        entry_price=ENTRY,
        current_price=_at_pct(ENTRY, 100.0),
        stored_peak_pct=None,
        completed_tier_indexes=(),
        partial_tiers=custom,
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
    )
    assert len(partials) == 1
    assert partials[0].tier_pct == 100.0
    assert partials[0].fraction == 0.5


# ---------------------------------------------------------------------------
# Price-scale anomaly guardrail (commit b92c9d1, response to cbBTC cascade)
# ---------------------------------------------------------------------------

def test_anomaly_normal_range_returns_false():
    """A 10x move is normal memecoin behavior — never anomaly."""
    assert is_price_scale_anomaly(entry_price=1.0, current_price=10.0) is False
    assert is_price_scale_anomaly(entry_price=10.0, current_price=1.0) is False


def test_anomaly_at_tier_4_threshold_returns_false():
    """Tier 4 of the partial-exit ladder is 99,900% peak_pct = 1000x.
    A legitimate climb to tier 4 must NOT trigger the anomaly guard."""
    assert is_price_scale_anomaly(entry_price=1.0, current_price=1000.0) is False


def test_anomaly_well_above_tier_4_returns_false_at_5000x():
    """5000x is rare but possible for true moonshots — still under
    the 10,000x guard."""
    assert is_price_scale_anomaly(entry_price=1.0, current_price=5000.0) is False


def test_anomaly_at_guard_boundary_returns_false():
    """The threshold uses STRICT > so the boundary value itself is
    NOT an anomaly. 10,000x exactly is treated as a (very rare) real move."""
    assert is_price_scale_anomaly(entry_price=1.0, current_price=10_000.0) is False


def test_anomaly_above_guard_returns_true():
    """One step above the guard fires."""
    assert is_price_scale_anomaly(entry_price=1.0, current_price=10_001.0) is True


def test_anomaly_inverse_ratio_also_fires():
    """The guard is direction-agnostic. If entry is 10,000x current
    (collapse to dust), that's ALSO a price-source bug, not a real
    move — fire the guard."""
    assert is_price_scale_anomaly(entry_price=10_001.0, current_price=1.0) is True


def test_anomaly_at_cbbtc_real_world_ratio_fires():
    """The actual ratio from the 2026-06-09 cbBTC cascade: bug-stored
    entry $0.000624, real Birdeye current $60,832. Ratio ~9.7×10^7."""
    assert is_price_scale_anomaly(
        entry_price=0.0006242655,
        current_price=60832.91,
    ) is True


def test_anomaly_zero_entry_returns_false():
    """Don't fire on uninitialized state — defer to caller's existing
    `entry_price > 0` check."""
    assert is_price_scale_anomaly(entry_price=0.0, current_price=100.0) is False


def test_anomaly_negative_entry_returns_false():
    assert is_price_scale_anomaly(entry_price=-1.0, current_price=100.0) is False


def test_anomaly_zero_current_returns_false():
    """Zero current price means an oracle dropout — the caller's
    `mid is None` check should handle it, but our guard shouldn't
    misfire on a literal 0.0."""
    assert is_price_scale_anomaly(entry_price=100.0, current_price=0.0) is False


def test_anomaly_custom_threshold_overrides_default():
    """The threshold is a parameter so tests + ops can tune it."""
    # Tighter threshold (1000x) catches what default doesn't
    assert is_price_scale_anomaly(
        entry_price=1.0, current_price=2000.0, ratio_threshold=1000.0
    ) is True
    # Default would NOT fire on 2000x
    assert is_price_scale_anomaly(entry_price=1.0, current_price=2000.0) is False


def test_anomaly_default_constant_is_10000x():
    """Pin the default so a future tweak is a deliberate decision."""
    assert PRICE_SCALE_ANOMALY_RATIO == 10_000.0
