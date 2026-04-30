"""Pure-function tests for STRUCTURE position sizing. No DB, no network."""
import pytest

from bots.structure.config import (
    PER_TRADE_NOTIONAL_CAP_PCT,
    SIGNAL_SPECS,
)
from bots.structure.sizing import size_position


PAPER = 1000.0


def test_funding_fade_low_conviction_uses_min_band():
    notional, lev = size_position("funding_fade", PAPER, conviction=0.0)
    spec = SIGNAL_SPECS["funding_fade"]
    assert lev == spec.leverage
    assert notional == pytest.approx(PAPER * spec.size_pct_min / 100, rel=1e-6)


def test_funding_fade_high_conviction_uses_max_band():
    notional, _ = size_position("funding_fade", PAPER, conviction=1.0)
    spec = SIGNAL_SPECS["funding_fade"]
    assert notional == pytest.approx(PAPER * spec.size_pct_max / 100, rel=1e-6)


def test_liquidation_cascade_uses_3x_leverage():
    _, lev = size_position("liquidation_cascade", PAPER, conviction=0.7)
    assert lev == 3.0


def test_whale_flip_uses_2x_leverage():
    _, lev = size_position("whale_flip", PAPER, conviction=0.5)
    assert lev == 2.0


def test_pre_leverage_cap_15pct():
    """Even if size_pct_max somehow exceeded 15%, cap kicks in."""
    notional, _ = size_position("liquidation_cascade", PAPER, conviction=1.0)
    cap = PAPER * PER_TRADE_NOTIONAL_CAP_PCT / 100
    assert notional <= cap + 1e-6


def test_drawdown_discount_at_halt_floor():
    """At full daily DD, sizing falls to 0.5x base."""
    base, _ = size_position("funding_fade", PAPER, current_dd_today_pct=0.0, conviction=1.0)
    halved, _ = size_position("funding_fade", PAPER, current_dd_today_pct=15.0, conviction=1.0)
    assert halved == pytest.approx(base * 0.5, rel=1e-6)


def test_drawdown_discount_intermediate():
    """At half-DD, discount is ~0.75x."""
    base, _ = size_position("funding_fade", PAPER, current_dd_today_pct=0.0, conviction=1.0)
    half_dd, _ = size_position("funding_fade", PAPER, current_dd_today_pct=7.5, conviction=1.0)
    assert half_dd == pytest.approx(base * 0.75, rel=1e-3)


def test_unknown_signal_type_raises():
    with pytest.raises(ValueError):
        size_position("not_a_signal", PAPER, conviction=0.5)


def test_conviction_clamped_to_unit():
    """Out-of-range conviction shouldn't blow up the math."""
    high, _ = size_position("funding_fade", PAPER, conviction=5.0)
    low, _ = size_position("funding_fade", PAPER, conviction=-2.0)
    spec = SIGNAL_SPECS["funding_fade"]
    assert high == pytest.approx(PAPER * spec.size_pct_max / 100, rel=1e-6)
    assert low == pytest.approx(PAPER * spec.size_pct_min / 100, rel=1e-6)
