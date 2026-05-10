"""Unit tests for Coinglass adapter + liquidation_cascade aggregate ingestion.

No network — _parse_liquidation_history is tested with synthetic payloads,
and the detector aggregate path is tested in isolation.
"""
import os

# Avoid pulling framework settings that require Postgres
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://nope:nope@localhost/nope")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import pytest

from bots.structure.coinglass import (
    CoinglassClient,
    LiquidationAggregate,
    _parse_liquidation_history,
)
from bots.structure.signals.liquidation_cascade import LiquidationCascadeDetector


# ---------- Coinglass payload parser ----------------------------------------

def test_parses_camelcase_v4_shape():
    payload = {
        "data": [
            {
                "time": 1730000000000,
                "longLiquidationUsd": 1_500_000.0,
                "shortLiquidationUsd": 250_000.0,
            },
            {
                "time": 1730000300000,
                "longLiquidationUsd": 800_000.0,
                "shortLiquidationUsd": 100_000.0,
            },
        ]
    }
    out = _parse_liquidation_history("BTC", "5m", payload)
    assert len(out) == 2
    assert out[0].bucket_start_ms == 1730000000000
    assert out[0].long_liq_usd == 1_500_000.0
    assert out[0].short_liq_usd == 250_000.0
    assert out[0].asset == "BTC"
    assert out[0].interval == "5m"
    # Sorted ascending by bucket
    assert out[1].bucket_start_ms == 1730000300000


def test_parses_snake_case_shape():
    """Some Coinglass endpoints use snake_case keys."""
    payload = {
        "data": [{"time": 100, "long_liquidation_usd": 5e6, "short_liquidation_usd": 2e6}]
    }
    out = _parse_liquidation_history("ETH", "5m", payload)
    assert len(out) == 1
    assert out[0].long_liq_usd == 5_000_000
    assert out[0].short_liq_usd == 2_000_000


def test_parses_nested_list_wrapper():
    """Coinglass v4 sometimes wraps as data: {list: [...]}."""
    payload = {
        "data": {"list": [
            {"time": 100, "longLiquidationUsd": 1e6, "shortLiquidationUsd": 0},
        ]}
    }
    out = _parse_liquidation_history("BTC", "5m", payload)
    assert len(out) == 1


def test_skips_malformed_rows():
    payload = {"data": [
        {"time": "not-a-number", "longLiquidationUsd": 1e6, "shortLiquidationUsd": 0},
        {"time": 100, "longLiquidationUsd": 1e6, "shortLiquidationUsd": 0},
        "garbage",
        {"time": 200},  # missing both liquidation fields → still parsed as 0/0
    ]}
    out = _parse_liquidation_history("BTC", "5m", payload)
    assert len(out) == 2  # row #2 + row #4 (with 0/0 values)


def test_returns_empty_on_non_dict_payload():
    assert _parse_liquidation_history("BTC", "5m", "garbage") == []
    assert _parse_liquidation_history("BTC", "5m", []) == []
    assert _parse_liquidation_history("BTC", "5m", {"data": "not-a-list"}) == []


# ---------- Auth gating -----------------------------------------------------

def test_client_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("COINGLASS_API_KEY", raising=False)
    client = CoinglassClient(api_key="")
    with pytest.raises(RuntimeError, match="COINGLASS_API_KEY"):
        client.liquidation_history("BTC")


# ---------- Detector aggregate path -----------------------------------------

def _ctx(asset="BTC", mid=80_000.0, oi_usd=100_000_000.0):
    from bots.structure.venue import AssetCtx
    return AssetCtx(
        asset=asset, mid_price=mid, funding_rate_hourly=0.0,
        open_interest_usd=oi_usd, day_volume_usd=1_000_000_000.0,
    )


def test_aggregate_path_fires_signal():
    """Aggregate ingestion should produce same signal as per-event path."""
    d = LiquidationCascadeDetector()
    asset = "BTC"
    now = 1_730_000_000_000
    base = now - 4 * 60 * 1000  # 4 min ago
    # $6M long liquidations + 4% price drop in window → LONG signal (fade longs liq'd)
    d.observe_aggregate(asset, base, long_liq_usd=6_000_000.0, short_liq_usd=0)
    d.observe_price(asset, 80_000.0, base)
    d.observe_price(asset, 76_500.0, now)  # -4.4% move
    candidates = d.evaluate([_ctx()], now_ms=now)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.signal_type == "liquidation_cascade"
    assert c.direction == "long"  # longs were liquidated → counter-trade long
    assert c.payload["long_liqs_usd"] == 6_000_000.0


def test_aggregate_dedup():
    """Re-pushing the same bucket must not double-count."""
    d = LiquidationCascadeDetector()
    asset = "BTC"
    bucket_ts = 1_730_000_000_000
    d.observe_aggregate(asset, bucket_ts, long_liq_usd=3_000_000.0, short_liq_usd=1_000_000.0)
    d.observe_aggregate(asset, bucket_ts, long_liq_usd=3_000_000.0, short_liq_usd=1_000_000.0)
    w = d._windows[asset]
    # 1 long + 1 short, not 4 entries
    assert len(w.liqs) == 2
    longs = [n for ts, side, n in w.liqs if side == "long"]
    shorts = [n for ts, side, n in w.liqs if side == "short"]
    assert longs == [3_000_000.0]
    assert shorts == [1_000_000.0]


def test_aggregate_zero_liq_skipped():
    """Buckets with 0 liquidations on a side should not push that pseudo-event."""
    d = LiquidationCascadeDetector()
    d.observe_aggregate("BTC", 100, long_liq_usd=0, short_liq_usd=2_000_000.0)
    w = d._windows["BTC"]
    assert len(w.liqs) == 1
    assert w.liqs[0][1] == "short"
