"""RA1 (2026-07-18) — depth-aware paper-exit fill price.

`liquidity_aware_exit_price` is now the SINGLE source for both full-close and
partial-tier fills. Before RA1 the partial path ignored depth entirely (raw mid
+ flat fee), so ladder-tier wins were booked with zero slippage. These pin the
function's behavior so that regression can't return silently.
"""
from __future__ import annotations

from bots.copy.fill_simulator import (
    EXIT_SLIPPAGE_BPS_DEFAULT,
    dex_fee_pct,
    liquidity_aware_exit_price,
)


MID = 2.0


def test_deep_pool_only_base_slippage():
    # $500 into a $1M pool: depth impact (0.0005) < base (0.01) -> base dominates.
    price, frac = liquidity_aware_exit_price(MID, 500.0, 1_000_000.0)
    assert abs(frac - EXIT_SLIPPAGE_BPS_DEFAULT / 10000.0) < 1e-9
    assert price == MID * (1.0 - frac)


def test_shallow_pool_takes_real_depth_impact():
    # $500 into a $1000 pool: 500/1500 = 0.333 slippage, far above base.
    price, frac = liquidity_aware_exit_price(MID, 500.0, 1_000.0)
    assert abs(frac - (500.0 / 1500.0)) < 1e-9
    assert price < MID * 0.7  # a real haircut, not a fictitious clean fill


def test_partial_slice_slips_more_than_raw_mid():
    # The exact regression: a slice sold into a thin pool must come out BELOW mid.
    price, frac = liquidity_aware_exit_price(MID, 800.0, 2_000.0)
    assert price < MID
    assert frac > EXIT_SLIPPAGE_BPS_DEFAULT / 10000.0


def test_unknown_liquidity_is_base_only():
    price, frac = liquidity_aware_exit_price(MID, 500.0, None)
    assert abs(frac - EXIT_SLIPPAGE_BPS_DEFAULT / 10000.0) < 1e-9


def test_zero_liquidity_is_total_loss():
    price, frac = liquidity_aware_exit_price(MID, 500.0, 0.0)
    assert frac == 1.0
    assert price == 0.0


def test_slippage_capped_near_total():
    # $500 into a $10 pool would be 0.98; capped at 0.97 so it's never a clean -100%.
    _, frac = liquidity_aware_exit_price(MID, 500.0, 10.0)
    assert frac == 0.97


# ---- RA2: mcap/liquidity-aware fee schedule ----

def test_fee_unknown_is_bonding_curve():
    assert dex_fee_pct() == 1.25  # unknown -> assume fresh/high-fee


def test_fee_small_cap_is_bonding_curve():
    assert dex_fee_pct(mcap_usd=50_000) == 1.25


def test_fee_large_cap_is_graduated():
    assert dex_fee_pct(mcap_usd=25_000_000) == 0.30
    assert dex_fee_pct(mcap_usd=100_000_000) == 0.30


def test_fee_mid_cap_between_and_monotonic():
    lo = dex_fee_pct(mcap_usd=200_000)
    hi = dex_fee_pct(mcap_usd=5_000_000)
    assert 0.30 < hi < lo < 1.25          # taper decreases with mcap


def test_fee_liquidity_fallback():
    # No mcap: liquidity*8 used as a mcap proxy. $10k liq -> $80k mcap -> bonding.
    assert dex_fee_pct(liquidity_usd=10_000) == 1.25
    # $500k liq -> $4M mcap -> mid-taper (strictly between the anchors).
    assert 0.30 < dex_fee_pct(liquidity_usd=500_000) < 1.25
