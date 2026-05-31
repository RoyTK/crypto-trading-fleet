"""Tests for shadow-log backtest replay logic.

Pure-function tests with synthetic ShadowLogRow inputs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from framework.backtest.shadow_log_replay import (
    ShadowExitConfig,
    ShadowLogRow,
    simulate_shadow_one,
    summarize_shadow,
)


def _row(
    *,
    entry: float = 100.0,
    p30m: float = 100.0,
    p1h: float = 100.0,
    p4h: float = 100.0,
    p12h: float = 100.0,
    mfe: float = 0.0,
    mae: float = 0.0,
) -> ShadowLogRow:
    return ShadowLogRow(
        id=1, cluster_uuid="t1", token_mint="MINT",
        cluster_size=3, wallet_tier="active",
        entry_price=entry, price_30m=p30m, price_1h=p1h,
        price_4h=p4h, price_12h=p12h,
        mfe_pct=mfe, mae_pct=mae,
        mfe_at=None, mae_at=None,
        fired_at=datetime.fromisoformat("2026-05-29T00:00:00+00:00"),
    )


def test_stop_fires_when_mae_below_threshold():
    row = _row(mfe=5.0, mae=-10.0)
    cfg = ShadowExitConfig(stop_pct=8.0, tp_pct=30.0, hold_hours=12.0)
    out = simulate_shadow_one(row, cfg, notional_usd=400.0)
    assert out is not None
    assert out.exit_window == "stop"
    assert out.exit_pct == pytest.approx(-8.0)
    assert out.pnl_usd == pytest.approx(-32.0)


def test_tp_fires_when_mfe_above_threshold():
    row = _row(mfe=50.0, mae=-5.0)
    cfg = ShadowExitConfig(stop_pct=8.0, tp_pct=30.0, hold_hours=12.0)
    out = simulate_shadow_one(row, cfg, notional_usd=400.0)
    assert out is not None
    assert out.exit_window == "tp"
    assert out.exit_pct == pytest.approx(30.0)
    assert out.pnl_usd == pytest.approx(120.0)


def test_both_hit_pessimistic_picks_stop():
    row = _row(mfe=50.0, mae=-15.0)
    cfg = ShadowExitConfig(stop_pct=8.0, tp_pct=30.0, hold_hours=12.0, tie_break="pessimistic")
    out = simulate_shadow_one(row, cfg, notional_usd=400.0)
    assert out is not None
    assert out.exit_window == "stop"


def test_both_hit_optimistic_picks_tp():
    row = _row(mfe=50.0, mae=-15.0)
    cfg = ShadowExitConfig(stop_pct=8.0, tp_pct=30.0, hold_hours=12.0, tie_break="optimistic")
    out = simulate_shadow_one(row, cfg, notional_usd=400.0)
    assert out is not None
    assert out.exit_window == "tp"


def test_no_thresholds_uses_12h_snapshot():
    row = _row(entry=100.0, p12h=125.0, mfe=30.0, mae=-2.0)
    cfg = ShadowExitConfig(stop_pct=None, tp_pct=None, hold_hours=12.0)
    out = simulate_shadow_one(row, cfg, notional_usd=400.0)
    assert out is not None
    assert out.exit_window == "hold_12h"
    assert out.exit_pct == pytest.approx(25.0)
    assert out.pnl_usd == pytest.approx(100.0)


def test_no_thresholds_uses_4h_snapshot_when_hold_4h():
    row = _row(entry=100.0, p1h=105.0, p4h=120.0, p12h=130.0, mfe=30.0, mae=-3.0)
    cfg = ShadowExitConfig(stop_pct=None, tp_pct=None, hold_hours=4.0)
    out = simulate_shadow_one(row, cfg, notional_usd=400.0)
    assert out is not None
    assert out.exit_window == "hold_4h"
    assert out.exit_pct == pytest.approx(20.0)


def test_disabled_tp_lets_winner_continue_to_snapshot():
    """Big winner: MFE 500%, MAE -3%. No TP. Use 12h snapshot."""
    row = _row(entry=100.0, p12h=400.0, mfe=500.0, mae=-3.0)
    cfg = ShadowExitConfig(stop_pct=8.0, tp_pct=None, hold_hours=12.0)
    out = simulate_shadow_one(row, cfg, notional_usd=400.0)
    assert out is not None
    # Stop not hit (MAE only -3 > -8). TP disabled. Use 12h snapshot = +300%
    assert out.exit_window == "hold_12h"
    assert out.exit_pct == pytest.approx(300.0)


def test_leverage_applies_to_pnl():
    row = _row(mfe=50.0, mae=-3.0)
    cfg = ShadowExitConfig(stop_pct=8.0, tp_pct=30.0, hold_hours=12.0, leverage=2.0)
    out = simulate_shadow_one(row, cfg, notional_usd=400.0)
    assert out is not None
    assert out.exit_window == "tp"
    assert out.pnl_pct == pytest.approx(60.0)
    assert out.pnl_usd == pytest.approx(240.0)


def test_summary_distribution():
    """A mix of 1 big winner + 3 stops + 1 flat — verify median and percentiles."""
    from framework.backtest.shadow_log_replay import ShadowResult
    res = [
        ShadowResult("a", "tp", 30.0, 30.0, 120.0),
        ShadowResult("b", "stop", -8.0, -8.0, -32.0),
        ShadowResult("c", "stop", -8.0, -8.0, -32.0),
        ShadowResult("d", "stop", -8.0, -8.0, -32.0),
        ShadowResult("e", "hold_12h", 0.0, 0.0, 0.0),
    ]
    summary = summarize_shadow(res)
    assert summary.n == 5
    assert summary.wins == 1
    assert summary.losses == 3
    assert summary.flat == 1
    assert summary.wr == 0.2
    assert summary.net_pnl_usd == pytest.approx(120.0 + 3 * (-32.0) + 0.0)
    # Median of [-8, -8, -8, 0, 30] is -8
    assert summary.median_pnl_pct == pytest.approx(-8.0)


def test_positive_skew_capture_demonstration():
    """Synthetic positive-skew dataset: most lose -8%, but 1 of 10 goes 50x.
    Compare baseline (caps at +30%) vs no-TP (captures 50x via 12h snapshot).
    """
    from framework.backtest.shadow_log_replay import replay_shadow
    # 9 losers at -10% MAE, 1 winner at MFE 5000% with 12h snapshot at +4000%
    rows = [_row(mfe=2.0, mae=-15.0) for _ in range(9)]
    rows.append(_row(entry=100.0, p12h=4100.0, mfe=5000.0, mae=-2.0))

    baseline = ShadowExitConfig(stop_pct=8.0, tp_pct=30.0, hold_hours=12.0)
    no_tp = ShadowExitConfig(stop_pct=8.0, tp_pct=None, hold_hours=12.0)

    _, base_summary = replay_shadow(rows, baseline, notional_usd=400.0)
    _, notp_summary = replay_shadow(rows, no_tp, notional_usd=400.0)

    # Baseline: 9 stops at -8% (-$32 each = -$288), 1 TP at +30% ($120). Net = -$168
    assert base_summary.net_pnl_usd == pytest.approx(-168.0)
    # No-TP: 9 stops at -8%, 1 12h-hold at +4000% on $400 = $16,000. Net = $15,712
    assert notp_summary.net_pnl_usd == pytest.approx(15712.0)
    # Same WR but vastly different PnL — that's the positive-skew capture
    assert base_summary.wr == notp_summary.wr
