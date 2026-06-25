"""Tests for the liquidity-aware paper-exit slippage model (pure function).

Replaces the old binary rug behavior (book either mid or a fictitious -100%):
selling a position into a thin pool now incurs depth-scaled slippage.
"""
import pytest

from bots.copy.main import _liquidity_aware_exit_price


MID = 1.0
SIZE = 400.0


def test_liquid_token_books_near_mid_at_base_slippage():
    price, frac = _liquidity_aware_exit_price(MID, SIZE, 1_000_000.0, base_slip_bps=100.0)
    assert frac == pytest.approx(0.01, abs=1e-4)   # depth impact ~0 → base 1%
    assert price == pytest.approx(MID * 0.99, rel=1e-4)


def test_thin_pool_scales_slippage_by_depth():
    # $400 into $294 liquidity → frac = 400/694 ≈ 0.576
    price, frac = _liquidity_aware_exit_price(MID, SIZE, 294.0, base_slip_bps=100.0)
    assert frac == pytest.approx(400.0 / 694.0, rel=1e-6)
    assert price == pytest.approx(MID * (1 - 400.0 / 694.0), rel=1e-6)


def test_very_thin_pool_near_total_loss_but_not_minus_100():
    # $400 into $26 liquidity → frac ≈ 0.939 (big loss, but not the old -100%)
    price, frac = _liquidity_aware_exit_price(MID, SIZE, 26.0)
    assert frac == pytest.approx(400.0 / 426.0, rel=1e-6)
    assert 0.9 < frac < 0.97


def test_unknown_liquidity_uses_base_slippage_not_rug():
    # None = unknown depth → must NOT book -100%; just base slippage.
    price, frac = _liquidity_aware_exit_price(MID, SIZE, None, base_slip_bps=100.0)
    assert frac == pytest.approx(0.01, abs=1e-4)
    assert price == pytest.approx(MID * 0.99, rel=1e-4)


def test_zero_liquidity_is_total_loss():
    price, frac = _liquidity_aware_exit_price(MID, SIZE, 0.0)
    assert frac == 1.0
    assert price == 0.0


def test_slippage_capped_below_one():
    # Tiny liquidity vs huge size → capped at 0.97 (never exactly -100% via slip)
    price, frac = _liquidity_aware_exit_price(MID, 10_000.0, 1.0)
    assert frac == pytest.approx(0.97)
    assert price == pytest.approx(MID * 0.03)


def test_base_slippage_is_a_floor():
    # Even a deep pool never books better than the base slippage.
    _, frac = _liquidity_aware_exit_price(MID, SIZE, 5_000_000.0, base_slip_bps=150.0)
    assert frac == pytest.approx(0.015, abs=1e-4)
