"""Tests for backtest signal-replay logic.

Pure-function tests with synthetic price series — no DB, no network.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from framework.backtest.signal_replay import (
    ExitConfig,
    SignalRow,
    simulate_one,
    summarize,
)


def _sig(direction: str = "long", asset: str = "BTC", t: str = "2026-01-01T00:00:00+00:00") -> SignalRow:
    return SignalRow(
        id=1, bot_id="test", signal_type="test", asset=asset, venue="hl",
        direction=direction, created_at=datetime.fromisoformat(t),
    )


def _series(start: str, prices: list[float], step_minutes: int = 15) -> list[tuple[datetime, float]]:
    base = datetime.fromisoformat(start)
    return [
        (base + timedelta(minutes=step_minutes * i), p)
        for i, p in enumerate(prices)
    ]


def test_long_tp_hit():
    sig = _sig("long", t="2026-01-01T00:00:00+00:00")
    # entry 100; +30% TP at 130; series goes 100 → 110 → 132 (hits TP)
    series = _series("2026-01-01T00:00:00+00:00", [100.0, 110.0, 132.0])
    cfg = ExitConfig(stop_pct=8.0, take_profit_pct=30.0, timeout_hours=12.0)
    result = simulate_one(sig, series, cfg, notional_usd=1000.0)
    assert result is not None
    assert result.exit_reason == "tp"
    assert result.exit_price == 132.0
    assert result.pnl_pct == pytest.approx(32.0, abs=0.01)  # (132-100)/100*100, no leverage


def test_long_stop_hit():
    sig = _sig("long")
    # 100 → 92 (hits -8% stop)
    series = _series("2026-01-01T00:00:00+00:00", [100.0, 95.0, 91.0])
    cfg = ExitConfig(stop_pct=8.0, take_profit_pct=30.0)
    result = simulate_one(sig, series, cfg, notional_usd=1000.0)
    assert result is not None
    assert result.exit_reason == "stop"
    assert result.pnl_pct == pytest.approx(-9.0, abs=0.01)


def test_short_tp_hit():
    sig = _sig("short")
    # Short entry 100; -30% TP at 70; price drops to 70
    series = _series("2026-01-01T00:00:00+00:00", [100.0, 85.0, 68.0])
    cfg = ExitConfig(stop_pct=8.0, take_profit_pct=30.0)
    result = simulate_one(sig, series, cfg, notional_usd=1000.0)
    assert result is not None
    assert result.exit_reason == "tp"
    # pnl_pct on short = -(exit-entry)/entry × 100 = -((68-100)/100)*100 = +32
    assert result.pnl_pct == pytest.approx(32.0, abs=0.01)


def test_short_stop_hit():
    sig = _sig("short")
    # Short entry 100; +8% stop at 108
    series = _series("2026-01-01T00:00:00+00:00", [100.0, 105.0, 109.0])
    cfg = ExitConfig(stop_pct=8.0, take_profit_pct=30.0)
    result = simulate_one(sig, series, cfg, notional_usd=1000.0)
    assert result is not None
    assert result.exit_reason == "stop"
    assert result.pnl_pct == pytest.approx(-9.0, abs=0.01)  # -(109-100)/100*100


def test_timeout_exit_uses_last_price_before_timeout():
    sig = _sig("long")
    # Entry 100; price drifts to 105 over 12h timeout, then beyond
    # Use 15-min steps × 50 ticks = 12.5h; last in-window price at ~12h
    prices = [100.0 + 0.1 * i for i in range(50)]  # 100..104.9
    series = _series("2026-01-01T00:00:00+00:00", prices, step_minutes=15)
    cfg = ExitConfig(stop_pct=10.0, take_profit_pct=30.0, timeout_hours=12.0)
    result = simulate_one(sig, series, cfg, notional_usd=1000.0)
    assert result is not None
    assert result.exit_reason == "timeout"
    # Last tick before 12h is at index 48 (15min × 48 = 720min = 12h)
    # Price at index 48 is 100 + 0.1*48 = 104.8
    assert result.pnl_pct == pytest.approx(4.8, abs=0.2)


def test_leverage_multiplies_pnl():
    sig = _sig("long")
    series = _series("2026-01-01T00:00:00+00:00", [100.0, 91.0])
    cfg = ExitConfig(stop_pct=8.0, take_profit_pct=30.0, leverage=2.0)
    result = simulate_one(sig, series, cfg, notional_usd=1000.0)
    assert result is not None
    assert result.exit_reason == "stop"
    assert result.pnl_pct == pytest.approx(-18.0, abs=0.01)  # 2x lev × -9%


def test_slippage_deducted():
    sig = _sig("long")
    series = _series("2026-01-01T00:00:00+00:00", [100.0, 130.0])
    cfg = ExitConfig(stop_pct=8.0, take_profit_pct=29.0, slippage_bps=100.0)
    # TP at +29% crossed by 130 → exit_price = 130. raw=30%, slippage 100bps=1pct → 29%
    result = simulate_one(sig, series, cfg, notional_usd=1000.0)
    assert result is not None
    assert result.exit_reason == "tp"
    assert result.pnl_pct == pytest.approx(29.0, abs=0.01)


def test_empty_price_series_returns_none():
    sig = _sig("long")
    cfg = ExitConfig()
    assert simulate_one(sig, [], cfg, notional_usd=1000.0) is None


def test_zero_entry_price_returns_none():
    sig = _sig("long")
    series = [(datetime.fromisoformat("2026-01-01T00:00:00+00:00"), 0.0)]
    cfg = ExitConfig()
    assert simulate_one(sig, series, cfg, notional_usd=1000.0) is None


def test_summarize_basic():
    sig = _sig("long")
    series_win = _series("2026-01-01T00:00:00+00:00", [100.0, 131.0])
    series_loss = _series("2026-01-01T00:00:00+00:00", [100.0, 91.0])
    cfg = ExitConfig(stop_pct=8.0, take_profit_pct=30.0)

    r_win = simulate_one(sig, series_win, cfg, 1000.0)
    r_loss = simulate_one(sig, series_loss, cfg, 1000.0)
    assert r_win is not None and r_loss is not None

    summary = summarize([r_win, r_loss], config=cfg)
    assert summary.n == 2
    assert summary.wins == 1
    assert summary.losses == 1
    assert summary.wr == 0.5
    assert summary.by_exit_reason == {"tp": 1, "stop": 1}


def test_summarize_empty():
    summary = summarize([])
    assert summary.n == 0
    assert summary.wr == 0.0
    assert summary.sharpe is None


def test_inverted_exits_long_with_tight_tp_wide_stop():
    """The H2 inversion thesis: -25% stop / +8% TP (long inverted)."""
    sig = _sig("long")
    # Price rises 8% (hits inverted TP)
    series_win = _series("2026-01-01T00:00:00+00:00", [100.0, 105.0, 109.0])
    cfg = ExitConfig(stop_pct=25.0, take_profit_pct=8.0)
    r = simulate_one(sig, series_win, cfg, 1000.0)
    assert r is not None
    assert r.exit_reason == "tp"
    # TP at 108; first price > 108 is 109 — exits at 109
    assert r.pnl_pct == pytest.approx(9.0, abs=0.01)

    # Series drops 25% before recovering
    series_loss = _series("2026-01-01T00:00:00+00:00", [100.0, 95.0, 74.0])
    r2 = simulate_one(sig, series_loss, cfg, 1000.0)
    assert r2 is not None
    assert r2.exit_reason == "stop"
    assert r2.pnl_pct == pytest.approx(-26.0, abs=0.01)
