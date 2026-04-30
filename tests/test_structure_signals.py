"""Tests for the 3 signal generators. No DB, no network."""
import pytest

from bots.structure.config import (
    FUNDING_LONG_THRESHOLD_PCT,
    FUNDING_OI_FLOOR_USD,
    FUNDING_SHORT_THRESHOLD_PCT,
    LIQ_NOTIONAL_THRESHOLD_USD,
    LIQ_PRICE_MOVE_THRESHOLD_PCT,
    WHALE_FLIP_THRESHOLD_USD,
)
from bots.structure.signals import funding_fade
from bots.structure.signals.liquidation_cascade import LiquidationCascadeDetector
from bots.structure.signals.whale_flip import WhaleFlipDetector
from bots.structure.venue import AssetCtx, LiquidationEvent, WhalePosition


HOURS_PER_YEAR = 24 * 365


def _ctx(
    asset="BTC",
    funding_pct_annualized: float = 0.0,
    oi_usd: float = 50_000_000.0,
    day_volume_usd: float = 1_000_000_000.0,
    mid_price: float = 100_000.0,
) -> AssetCtx:
    """Build an AssetCtx given an annualized funding pct (human-friendly)."""
    funding_per_hour = funding_pct_annualized / HOURS_PER_YEAR / 100.0
    return AssetCtx(
        asset=asset,
        mid_price=mid_price,
        funding_rate_hourly=funding_per_hour,
        open_interest_usd=oi_usd,
        day_volume_usd=day_volume_usd,
    )


# ---- Funding Fade ----------------------------------------------------------

def test_funding_fade_above_threshold_fires_short():
    ctx = _ctx(funding_pct_annualized=FUNDING_SHORT_THRESHOLD_PCT + 10.0)
    out = funding_fade.evaluate([ctx])
    assert len(out) == 1
    assert out[0].direction == "short"
    assert out[0].signal_type == "funding_fade"
    assert out[0].asset == "BTC"


def test_funding_fade_below_threshold_fires_long():
    ctx = _ctx(funding_pct_annualized=FUNDING_LONG_THRESHOLD_PCT - 10.0)
    out = funding_fade.evaluate([ctx])
    assert len(out) == 1
    assert out[0].direction == "long"


def test_funding_fade_within_band_does_not_fire():
    ctx = _ctx(funding_pct_annualized=20.0)
    out = funding_fade.evaluate([ctx])
    assert out == []


def test_funding_fade_low_oi_filtered_out():
    ctx = _ctx(
        funding_pct_annualized=FUNDING_SHORT_THRESHOLD_PCT + 50.0,
        oi_usd=FUNDING_OI_FLOOR_USD - 1.0,
    )
    out = funding_fade.evaluate([ctx])
    assert out == []


def test_funding_fade_only_top_volume_assets():
    """If candidate falls outside top-20 by volume, filtered out."""
    fired = _ctx(asset="MAIN", funding_pct_annualized=100.0, day_volume_usd=1)
    fillers = [
        _ctx(asset=f"FILL{i}", funding_pct_annualized=0.0, day_volume_usd=10_000_000_000.0)
        for i in range(25)
    ]
    out = funding_fade.evaluate(fillers + [fired])
    assert all(c.asset != "MAIN" for c in out)


# ---- Liquidation Cascade ---------------------------------------------------

def test_liquidation_cascade_fires_on_threshold_breach():
    det = LiquidationCascadeDetector()
    ctx = _ctx(asset="ETH", mid_price=3000.0)
    # Simulate 6M of long-side liquidations + 5% price drop in 5 min
    base_ts = 1_000_000
    det.observe_price("ETH", 3000.0, base_ts)
    det.observe_price("ETH", 2850.0, base_ts + 60_000)  # -5% move
    det.observe_liquidation(LiquidationEvent(
        asset="ETH", side="long",
        notional_usd=LIQ_NOTIONAL_THRESHOLD_USD * 1.5,
        price=2900.0, timestamp_ms=base_ts + 30_000,
    ))
    out = det.evaluate([ctx], now_ms=base_ts + 60_000)
    assert len(out) == 1
    assert out[0].asset == "ETH"
    assert out[0].direction == "long"  # counter the long-liq cascade
    assert out[0].signal_type == "liquidation_cascade"


def test_liquidation_cascade_below_threshold_does_not_fire():
    det = LiquidationCascadeDetector()
    ctx = _ctx(asset="ETH", mid_price=3000.0)
    det.observe_price("ETH", 3000.0, 1_000_000)
    det.observe_price("ETH", 2850.0, 1_060_000)
    det.observe_liquidation(LiquidationEvent(
        asset="ETH", side="long",
        notional_usd=LIQ_NOTIONAL_THRESHOLD_USD * 0.5,
        price=2900.0, timestamp_ms=1_030_000,
    ))
    out = det.evaluate([ctx], now_ms=1_060_000)
    assert out == []


def test_liquidation_cascade_no_price_move_does_not_fire():
    det = LiquidationCascadeDetector()
    ctx = _ctx(asset="ETH", mid_price=3000.0)
    det.observe_price("ETH", 3000.0, 1_000_000)
    det.observe_price("ETH", 3001.0, 1_060_000)  # < 4% threshold
    det.observe_liquidation(LiquidationEvent(
        asset="ETH", side="long",
        notional_usd=LIQ_NOTIONAL_THRESHOLD_USD * 2,
        price=3000.0, timestamp_ms=1_030_000,
    ))
    out = det.evaluate([ctx], now_ms=1_060_000)
    assert out == []


def test_liquidation_cascade_skips_low_volume_assets():
    """An asset outside top-15 should not fire even with breach."""
    det = LiquidationCascadeDetector()
    main_ctx = _ctx(asset="OBSCURE", day_volume_usd=1)
    fillers = [_ctx(asset=f"FILL{i}", day_volume_usd=10_000_000_000.0) for i in range(20)]
    det.observe_price("OBSCURE", 100.0, 1_000_000)
    det.observe_price("OBSCURE", 95.0, 1_060_000)
    det.observe_liquidation(LiquidationEvent(
        asset="OBSCURE", side="long",
        notional_usd=LIQ_NOTIONAL_THRESHOLD_USD * 2,
        price=98.0, timestamp_ms=1_030_000,
    ))
    out = det.evaluate(fillers + [main_ctx], now_ms=1_060_000)
    assert all(c.asset != "OBSCURE" for c in out)


# ---- Whale Flip ------------------------------------------------------------

def test_whale_flip_long_to_short_fires_short():
    det = WhaleFlipDetector()
    addr = "0xWHALE1"
    # Prior poll: long $1M
    det.observe_positions(
        addr,
        [WhalePosition(asset="BTC", size_native=10.0, notional_usd=1_000_000.0)],
        now_ts_ms=1_000_000,
    )
    # Drain — should be empty (no flip yet, this is the establishing observation)
    assert det.evaluate() == []
    # Next poll: now short $1M
    det.observe_positions(
        addr,
        [WhalePosition(asset="BTC", size_native=-10.0, notional_usd=-1_000_000.0)],
        now_ts_ms=1_060_000,
    )
    out = det.evaluate()
    assert len(out) == 1
    assert out[0].direction == "short"
    assert out[0].signal_type == "whale_flip"


def test_whale_flip_short_to_long_fires_long():
    det = WhaleFlipDetector()
    addr = "0xWHALE2"
    det.observe_positions(
        addr,
        [WhalePosition(asset="ETH", size_native=-100.0, notional_usd=-800_000.0)],
        now_ts_ms=1_000_000,
    )
    det.evaluate()  # drain
    det.observe_positions(
        addr,
        [WhalePosition(asset="ETH", size_native=100.0, notional_usd=800_000.0)],
        now_ts_ms=1_060_000,
    )
    out = det.evaluate()
    assert len(out) == 1
    assert out[0].direction == "long"


def test_whale_flip_below_notional_threshold_does_not_fire():
    det = WhaleFlipDetector()
    addr = "0xSMALL"
    small = WHALE_FLIP_THRESHOLD_USD / 4
    det.observe_positions(addr, [WhalePosition("BTC", 1.0, small)], now_ts_ms=1_000_000)
    det.evaluate()
    det.observe_positions(addr, [WhalePosition("BTC", -1.0, -small)], now_ts_ms=1_060_000)
    out = det.evaluate()
    assert out == []


def test_whale_flip_close_to_zero_is_not_flip():
    """Closing a position is not a flip — direction goes to 0, not opposite."""
    det = WhaleFlipDetector()
    addr = "0xCLOSER"
    det.observe_positions(
        addr,
        [WhalePosition("BTC", 10.0, 1_000_000.0)],
        now_ts_ms=1_000_000,
    )
    det.evaluate()
    det.observe_positions(addr, [], now_ts_ms=1_060_000)  # closed all
    out = det.evaluate()
    assert out == []


def test_whale_flip_same_direction_increase_does_not_fire():
    det = WhaleFlipDetector()
    addr = "0xADDER"
    det.observe_positions(
        addr,
        [WhalePosition("BTC", 10.0, 1_000_000.0)],
        now_ts_ms=1_000_000,
    )
    det.evaluate()
    det.observe_positions(
        addr,
        [WhalePosition("BTC", 20.0, 2_000_000.0)],
        now_ts_ms=1_060_000,
    )
    out = det.evaluate()
    assert out == []
