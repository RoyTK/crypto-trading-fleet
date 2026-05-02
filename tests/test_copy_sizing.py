"""Pure-function tests for COPY position sizing. No DB, no network."""
import pytest

from bots.copy.config import (
    ALLOCATION_CAP_PCT,
    PER_TRADE_NOTIONAL_CAP_PCT,
    cluster_size_to_pct,
)
from bots.copy.sizing import size_position


PAPER = 1000.0


def test_cluster_size_to_pct_3_wallets_4pct():
    assert cluster_size_to_pct(3) == 4.0


def test_cluster_size_to_pct_4_wallets_6pct():
    assert cluster_size_to_pct(4) == 6.0


def test_cluster_size_to_pct_5_wallets_6pct():
    assert cluster_size_to_pct(5) == 6.0


def test_cluster_size_to_pct_6plus_wallets_8pct():
    assert cluster_size_to_pct(6) == 8.0
    assert cluster_size_to_pct(20) == 8.0


def test_cluster_size_below_min_returns_zero():
    assert cluster_size_to_pct(2) == 0.0
    assert cluster_size_to_pct(0) == 0.0


def test_size_position_3_wallet_cluster_no_alloc():
    notional = size_position(cluster_size=3, paper_capital_usd=PAPER)
    assert notional == pytest.approx(PAPER * 0.04, rel=1e-6)


def test_size_position_6_wallet_cluster_at_per_trade_cap():
    notional = size_position(cluster_size=6, paper_capital_usd=PAPER)
    cap = PAPER * PER_TRADE_NOTIONAL_CAP_PCT / 100
    assert notional == pytest.approx(cap, rel=1e-6)


def test_allocation_cap_shrinks_to_headroom():
    # Already 47% allocated → only 3% room before hitting the 50% cap
    notional = size_position(cluster_size=6, paper_capital_usd=PAPER, current_open_alloc_pct=47.0)
    expected = PAPER * 0.03
    assert notional == pytest.approx(expected, rel=1e-6)


def test_allocation_cap_full_returns_zero():
    notional = size_position(cluster_size=6, paper_capital_usd=PAPER, current_open_alloc_pct=ALLOCATION_CAP_PCT)
    assert notional == 0.0


def test_drawdown_discount_at_halt_floor():
    base = size_position(cluster_size=6, paper_capital_usd=PAPER, current_dd_today_pct=0.0)
    halved = size_position(cluster_size=6, paper_capital_usd=PAPER, current_dd_today_pct=12.0)
    assert halved == pytest.approx(base * 0.5, rel=1e-6)


def test_drawdown_discount_intermediate():
    base = size_position(cluster_size=6, paper_capital_usd=PAPER, current_dd_today_pct=0.0)
    half_dd = size_position(cluster_size=6, paper_capital_usd=PAPER, current_dd_today_pct=6.0)
    assert half_dd == pytest.approx(base * 0.75, rel=1e-3)


def test_below_min_cluster_returns_zero():
    notional = size_position(cluster_size=2, paper_capital_usd=PAPER)
    assert notional == 0.0
