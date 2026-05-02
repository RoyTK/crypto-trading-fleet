"""Tests for STRUCTURE shadow executor — pure-function paths only.

DB-touching paths (DB writes, calibration_records pairing) and live HL Exchange
calls are exercised on Hetzner during deploy + shakedown gate.
"""
import pytest
from unittest.mock import MagicMock

from bots.structure.executor import (
    SHADOW_NOTIONAL_MAX_USD,
    SHADOW_NOTIONAL_MIN_USD,
    StructureExecutor,
    _shadow_notional_for,
)


# ---------------------------------------------------------------------------
# _shadow_notional_for: paper $ → shadow $ mapping
# ---------------------------------------------------------------------------

def test_shadow_notional_below_floor_clamps_to_min():
    # Paper $1000 * 0.001 = $1, below min
    assert _shadow_notional_for(1000.0) == SHADOW_NOTIONAL_MIN_USD


def test_shadow_notional_above_ceiling_clamps_to_max():
    # Paper $50000 * 0.001 = $50, above max ($20)
    assert _shadow_notional_for(50_000.0) == SHADOW_NOTIONAL_MAX_USD


def test_shadow_notional_within_band_uses_linear():
    # Paper $15000 * 0.001 = $15, within band [11, 20]
    assert _shadow_notional_for(15_000.0) == 15.0


def test_shadow_notional_at_floor_boundary():
    # $11000 * 0.001 = $11 exactly = min
    assert _shadow_notional_for(11_000.0) == SHADOW_NOTIONAL_MIN_USD


def test_shadow_notional_at_ceiling_boundary():
    # $20000 * 0.001 = $20 exactly = max
    assert _shadow_notional_for(20_000.0) == SHADOW_NOTIONAL_MAX_USD


# ---------------------------------------------------------------------------
# _parse_order_response: response → (px, sz, oid)
# ---------------------------------------------------------------------------

@pytest.fixture
def executor():
    venue = MagicMock()
    return StructureExecutor(venue)


def test_parse_filled_response(executor):
    resp = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"filled": {"totalSz": "0.05", "avgPx": "1999.5", "oid": 12345}}]},
        },
    }
    px, sz, oid = executor._parse_order_response(resp)
    assert px == pytest.approx(1999.5)
    assert sz == pytest.approx(0.05)
    assert oid == 12345


def test_parse_resting_response_returns_none(executor):
    """IoC that doesn't fill — order rests momentarily, gets canceled. Treat as no-fill."""
    resp = {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 999}}]}},
    }
    px, sz, oid = executor._parse_order_response(resp)
    assert px is None
    assert sz == 0.0
    assert oid is None


def test_parse_error_response_returns_none(executor):
    resp = {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{"error": "MinTradeNtl"}]}},
    }
    px, sz, oid = executor._parse_order_response(resp)
    assert px is None


def test_parse_top_level_error_returns_none(executor):
    resp = {"status": "err", "response": "Insufficient margin"}
    px, sz, oid = executor._parse_order_response(resp)
    assert px is None


def test_parse_malformed_response_returns_none(executor):
    """Garbage in → no crash, no false fill."""
    for bad in [None, "string", 42, [], {"status": "ok"}, {}]:
        px, sz, oid = executor._parse_order_response(bad)
        assert px is None
        assert sz == 0.0
        assert oid is None


# ---------------------------------------------------------------------------
# maybe_place_shadow: sampling + agent-key gate
# ---------------------------------------------------------------------------

def test_maybe_place_shadow_skips_at_zero_pct(executor, monkeypatch):
    """If STRUCTURE_SHADOW_PCT=0, never fires regardless of sampling."""
    executor.settings = MagicMock()
    executor.settings.structure_shadow_pct = 0.0
    candidate = MagicMock()
    sim_fill = MagicMock()
    out = executor.maybe_place_shadow(
        signal_id=1, paper_trade_id=2, candidate=candidate,
        paper_sim_fill=sim_fill, paper_notional_usd=100.0,
    )
    assert out is None


def test_maybe_place_shadow_skips_when_no_agent_key(executor, monkeypatch):
    """No agent key → no shadow."""
    executor.settings = MagicMock()
    executor.settings.structure_shadow_pct = 100.0  # always sample
    monkeypatch.setattr("bots.structure.executor.is_exchange_available", lambda: False)
    candidate = MagicMock()
    sim_fill = MagicMock()
    out = executor.maybe_place_shadow(
        signal_id=1, paper_trade_id=2, candidate=candidate,
        paper_sim_fill=sim_fill, paper_notional_usd=100.0,
    )
    assert out is None
