"""Unit tests for the OI Divergence detector.

Pure-logic tests with synthetic AssetCtx objects + deterministic timestamps —
no DB, no network. Covers happy path (both legs), threshold gates, native-vs-
USD OI conflation guard, cold-start guard, and debounce.
"""
import os

# Avoid pulling in framework settings that require Postgres at import time
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://nope:nope@localhost/nope")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from bots.structure.signals.hl_oi_divergence import OIDivergenceDetector, WINDOW_MS
from bots.structure.venue import AssetCtx


def _ctx(asset, mid_price, oi_usd, day_vol_usd=1_000_000_000.0):
    """Build an AssetCtx that's in top-15 (huge volume) by default."""
    return AssetCtx(
        asset=asset,
        mid_price=mid_price,
        funding_rate_hourly=0.0,
        open_interest_usd=oi_usd,
        day_volume_usd=day_vol_usd,
    )


def _seed_window(detector, asset, samples):
    """Push a list of (ts_ms, oi_usd, mid_price) snapshots into detector buffer."""
    for ts, oi_usd, px in samples:
        detector.observe_snapshot(asset, oi_usd, px, timestamp_ms=ts)


# ---------- Happy path -------------------------------------------------------

def test_long_signal_fires_on_price_down_oi_up():
    """Price down -2.5% + OI up 25% native = fresh shorts → LONG."""
    d = OIDivergenceDetector()
    asset = "BTC"
    now = 10_000_000_000_000
    base_ts = now - WINDOW_MS  # exactly 4h ago
    # Baseline: 100 BTC OI at $80k => $8M USD oi, but we need oi_floor met.
    # Use native count of 1000 BTC at $80k = $80M USD OI.
    _seed_window(d, asset, [
        (base_ts, 1000 * 80_000.0, 80_000.0),       # native=1000, px=80k
        (now,     1250 * 78_000.0, 78_000.0),       # native=1250 (+25%), px=78k (-2.5%)
    ])
    candidates = d.evaluate([_ctx(asset, 78_000.0, 1250 * 78_000.0)], now_ms=now)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.signal_type == "hl_oi_divergence"
    assert c.direction == "long"
    assert c.payload["trigger"] == "fresh_shorts"
    assert c.payload["oi_delta_pct"] == 25.0
    assert c.payload["price_delta_pct"] == -2.5


def test_short_signal_fires_on_price_up_oi_up():
    """Price up +5% + OI up 30% native = fresh longs → SHORT."""
    d = OIDivergenceDetector()
    asset = "ETH"
    now = 10_000_000_000_000
    base_ts = now - WINDOW_MS
    _seed_window(d, asset, [
        (base_ts, 10_000 * 3_000.0, 3_000.0),
        (now,     13_000 * 3_150.0, 3_150.0),  # native +30%, px +5%
    ])
    candidates = d.evaluate([_ctx(asset, 3_150.0, 13_000 * 3_150.0)], now_ms=now)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.direction == "short"
    assert c.payload["trigger"] == "fresh_longs"


# ---------- Native-vs-USD OI conflation guard -------------------------------

def test_oi_growth_in_usd_only_does_not_fire():
    """OI USD grows but native count is flat (price moved) — must NOT fire."""
    d = OIDivergenceDetector()
    asset = "SOL"
    now = 10_000_000_000_000
    base_ts = now - WINDOW_MS
    # native count fixed at 50k. Price 100 -> 130 (+30% USD OI from price alone)
    _seed_window(d, asset, [
        (base_ts, 50_000 * 100.0, 100.0),
        (now,     50_000 * 130.0, 130.0),
    ])
    candidates = d.evaluate([_ctx(asset, 130.0, 50_000 * 130.0)], now_ms=now)
    assert candidates == [], "OI USD grew via price, not contract count — should not fire"


# ---------- Threshold gates --------------------------------------------------

def test_below_oi_delta_threshold_no_signal():
    """OI grows only 15% (below 20% threshold) — no signal."""
    d = OIDivergenceDetector()
    asset = "BTC"
    now = 10_000_000_000_000
    base_ts = now - WINDOW_MS
    _seed_window(d, asset, [
        (base_ts, 1000 * 80_000.0, 80_000.0),
        (now,     1150 * 78_000.0, 78_000.0),  # native +15%
    ])
    candidates = d.evaluate([_ctx(asset, 78_000.0, 1150 * 78_000.0)], now_ms=now)
    assert candidates == []


def test_below_price_delta_threshold_no_signal():
    """Price moves only 1% (below 2% threshold) even with big OI move — no signal."""
    d = OIDivergenceDetector()
    asset = "BTC"
    now = 10_000_000_000_000
    base_ts = now - WINDOW_MS
    _seed_window(d, asset, [
        (base_ts, 1000 * 80_000.0, 80_000.0),
        (now,     1300 * 79_200.0, 79_200.0),  # native +30%, px -1%
    ])
    candidates = d.evaluate([_ctx(asset, 79_200.0, 1300 * 79_200.0)], now_ms=now)
    assert candidates == []


def test_oi_floor_filters_thin_markets():
    """Asset with current OI < $10M floor — no signal even on big divergence."""
    d = OIDivergenceDetector()
    asset = "TINY"
    now = 10_000_000_000_000
    base_ts = now - WINDOW_MS
    _seed_window(d, asset, [
        (base_ts, 100_000 * 1.0, 1.0),     # $100k OI
        (now,     200_000 * 0.95, 0.95),   # native +100%, px -5%
    ])
    candidates = d.evaluate([_ctx(asset, 0.95, 200_000 * 0.95)], now_ms=now)
    assert candidates == []


def test_top_vol_filter():
    """Asset not in top-15 by volume — no signal."""
    d = OIDivergenceDetector()
    asset = "OBSCURE"
    now = 10_000_000_000_000
    base_ts = now - WINDOW_MS
    _seed_window(d, asset, [
        (base_ts, 1000 * 80_000.0, 80_000.0),
        (now,     1300 * 78_000.0, 78_000.0),
    ])
    # Provide 16 fake high-volume assets, OBSCURE has tiny volume → not in top-15
    other_ctxs = [
        AssetCtx(asset=f"BIG{i}", mid_price=100.0, funding_rate_hourly=0.0,
                 open_interest_usd=100_000_000, day_volume_usd=1_000_000_000)
        for i in range(15)
    ]
    obscure_ctx = _ctx(asset, 78_000.0, 1300 * 78_000.0, day_vol_usd=1_000.0)
    candidates = d.evaluate(other_ctxs + [obscure_ctx], now_ms=now)
    assert candidates == []


# ---------- Cold start + window coverage ------------------------------------

def test_cold_start_no_signal_with_only_one_snapshot():
    d = OIDivergenceDetector()
    asset = "BTC"
    now = 10_000_000_000_000
    _seed_window(d, asset, [(now, 1000 * 80_000.0, 80_000.0)])
    candidates = d.evaluate([_ctx(asset, 80_000.0, 1000 * 80_000.0)], now_ms=now)
    assert candidates == []


def test_partial_window_no_signal():
    """Only 1h of buffer (vs 4h window) — must not fire."""
    d = OIDivergenceDetector()
    asset = "BTC"
    now = 10_000_000_000_000
    base_ts = now - 1 * 3600 * 1000  # 1h ago
    _seed_window(d, asset, [
        (base_ts, 1000 * 80_000.0, 80_000.0),
        (now,     1300 * 78_000.0, 78_000.0),
    ])
    candidates = d.evaluate([_ctx(asset, 78_000.0, 1300 * 78_000.0)], now_ms=now)
    assert candidates == []


# ---------- Debounce ---------------------------------------------------------

def test_debounce_prevents_repeat_within_window():
    """After firing, must not re-fire while window still active."""
    d = OIDivergenceDetector()
    asset = "BTC"
    now = 10_000_000_000_000
    base_ts = now - WINDOW_MS
    _seed_window(d, asset, [
        (base_ts, 1000 * 80_000.0, 80_000.0),
        (now,     1250 * 78_000.0, 78_000.0),
    ])
    ctx = _ctx(asset, 78_000.0, 1250 * 78_000.0)
    first = d.evaluate([ctx], now_ms=now)
    assert len(first) == 1

    # 1 hour later, conditions still meet — should be debounced
    later = now + 3600 * 1000
    d.observe_snapshot(asset, 1300 * 77_000.0, 77_000.0, timestamp_ms=later)
    second = d.evaluate([_ctx(asset, 77_000.0, 1300 * 77_000.0)], now_ms=later)
    assert second == []


def test_can_re_fire_after_window_elapses():
    d = OIDivergenceDetector()
    asset = "BTC"
    now = 10_000_000_000_000
    base_ts = now - WINDOW_MS
    _seed_window(d, asset, [
        (base_ts, 1000 * 80_000.0, 80_000.0),
        (now,     1250 * 78_000.0, 78_000.0),
    ])
    ctx = _ctx(asset, 78_000.0, 1250 * 78_000.0)
    first = d.evaluate([ctx], now_ms=now)
    assert len(first) == 1

    # 5h later (past the WINDOW_MS debounce), with fresh divergence → re-fire allowed
    later = now + WINDOW_MS + 1000  # just past debounce
    new_base_ts = later - WINDOW_MS
    _seed_window(d, asset, [
        (new_base_ts, 1300 * 76_000.0, 76_000.0),
        (later,       1700 * 73_500.0, 73_500.0),  # native +30%, px -3.3%
    ])
    second = d.evaluate([_ctx(asset, 73_500.0, 1700 * 73_500.0)], now_ms=later)
    assert len(second) == 1


# ---------- Sizing fields ----------------------------------------------------

def test_signal_carries_exit_thresholds():
    d = OIDivergenceDetector()
    asset = "BTC"
    now = 10_000_000_000_000
    base_ts = now - WINDOW_MS
    _seed_window(d, asset, [
        (base_ts, 1000 * 80_000.0, 80_000.0),
        (now,     1250 * 78_000.0, 78_000.0),
    ])
    candidates = d.evaluate([_ctx(asset, 78_000.0, 1250 * 78_000.0)], now_ms=now)
    c = candidates[0]
    assert c.stop_pct == 5.0
    assert c.take_profit_pct == 4.0
    assert c.timeout_hours == 12
