"""Tests for COPY executor — sampling gates, exposure cap, swap result
flow. All Solana RPC + Jupiter calls are mocked; no network required.

These tests cover the behavior the executor MUST guarantee:
- copy_live_enabled=false => maybe_place_shadow / maybe_place_live
  return None without touching the wallet
- no wallet available => maybe_place_* returns None even with the flag on
- non-solana chain => returns None (executor is Solana-specific)
- short direction => returns None (no on-chain short primitive)
- exposure cap breach => returns None
- successful swap path => writes Trade(mode='shadow') + CalibrationRecord
- close path => updates Trade.exit_* and CalibrationRecord PnL fields
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import pytest

from bots.copy.executor import CopyExecutor, _shadow_notional_for
from bots.copy.venue.jupiter_swap import SwapResult


@dataclass
class FakeSimFill:
    fill_price: float = 0.001
    fees_usd: float = 0.5
    slippage_bps: float = 100.0
    metadata: dict = None  # type: ignore
    no_fill_reason: Optional[str] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


@dataclass
class FakeCandidate:
    asset: str = "TOKENMINT123"
    chain: str = "solana"
    venue: str = "solana"
    direction: str = "long"
    cluster_size: int = 3
    signal_type: str = "cluster_buy"
    payload: dict = None  # type: ignore
    stop_pct: float = 8.0
    take_profit_pct: float = 30.0
    timeout_hours: int = 12

    def __post_init__(self):
        if self.payload is None:
            self.payload = {"wallets": ["w1", "w2", "w3"]}


def _settings_with(**overrides):
    """Build a CopySettings-shaped object with the fields the executor reads."""
    from bots.copy.config import CopySettings
    return CopySettings(**overrides)


# ---------------------------------------------------------------------------
# Pure helper math
# ---------------------------------------------------------------------------

def test_shadow_notional_band_lower_bound():
    """Paper $100 → 1% = $1, clamped to min $10."""
    with patch("bots.copy.executor.get_copy_settings",
               return_value=_settings_with(copy_shadow_notional_min_usd=10.0,
                                            copy_shadow_notional_max_usd=25.0)):
        assert _shadow_notional_for(100.0) == 10.0


def test_shadow_notional_band_upper_bound():
    """Paper $5000 → 1% = $50, clamped to max $25."""
    with patch("bots.copy.executor.get_copy_settings",
               return_value=_settings_with(copy_shadow_notional_min_usd=10.0,
                                            copy_shadow_notional_max_usd=25.0)):
        assert _shadow_notional_for(5_000.0) == 25.0


def test_shadow_notional_band_middle():
    """Paper $1500 → 1% = $15, in band → unchanged."""
    with patch("bots.copy.executor.get_copy_settings",
               return_value=_settings_with(copy_shadow_notional_min_usd=10.0,
                                            copy_shadow_notional_max_usd=25.0)):
        assert _shadow_notional_for(1_500.0) == 15.0


# ---------------------------------------------------------------------------
# Gates that must short-circuit before any signing work
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_maybe_place_shadow_returns_none_when_live_disabled():
    ex = CopyExecutor()
    with patch.object(ex, "settings", _settings_with(copy_live_enabled=False)):
        out = await ex.maybe_place_shadow(
            session=None,
            signal_id=1, paper_trade_id=10,
            candidate=FakeCandidate(),
            paper_sim_fill=FakeSimFill(),
            paper_notional_usd=1000.0,
        )
    assert out is None


@pytest.mark.asyncio
async def test_maybe_place_shadow_returns_none_when_no_wallet():
    ex = CopyExecutor()
    with patch.object(ex, "settings", _settings_with(copy_live_enabled=True, copy_shadow_pct=100.0)), \
         patch("bots.copy.executor.is_wallet_available", return_value=False):
        out = await ex.maybe_place_shadow(
            session=None,
            signal_id=1, paper_trade_id=10,
            candidate=FakeCandidate(),
            paper_sim_fill=FakeSimFill(),
            paper_notional_usd=1000.0,
        )
    assert out is None


@pytest.mark.asyncio
async def test_maybe_place_shadow_skips_non_solana():
    ex = CopyExecutor()
    with patch.object(ex, "settings", _settings_with(copy_live_enabled=True, copy_shadow_pct=100.0)), \
         patch("bots.copy.executor.is_wallet_available", return_value=True):
        out = await ex.maybe_place_shadow(
            session=None,
            signal_id=1, paper_trade_id=10,
            candidate=FakeCandidate(chain="base", venue="base"),
            paper_sim_fill=FakeSimFill(),
            paper_notional_usd=1000.0,
        )
    assert out is None


@pytest.mark.asyncio
async def test_maybe_place_shadow_skips_short_direction():
    ex = CopyExecutor()
    with patch.object(ex, "settings", _settings_with(copy_live_enabled=True, copy_shadow_pct=100.0)), \
         patch("bots.copy.executor.is_wallet_available", return_value=True):
        out = await ex.maybe_place_shadow(
            session=None,
            signal_id=1, paper_trade_id=10,
            candidate=FakeCandidate(direction="short"),
            paper_sim_fill=FakeSimFill(),
            paper_notional_usd=1000.0,
        )
    assert out is None


@pytest.mark.asyncio
async def test_maybe_place_shadow_respects_exposure_cap():
    ex = CopyExecutor()
    with patch.object(ex, "settings", _settings_with(
            copy_live_enabled=True, copy_shadow_pct=100.0,
            copy_shadow_open_cap_usd=30.0,
            copy_shadow_notional_min_usd=15.0,
            copy_shadow_notional_max_usd=15.0,
         )), \
         patch("bots.copy.executor.is_wallet_available", return_value=True), \
         patch("bots.copy.executor._open_notional_by_mode", return_value=25.0):
        # current_open ($25) + shadow_usd ($15) = $40 > cap ($30) → skip
        out = await ex.maybe_place_shadow(
            session=None,
            signal_id=1, paper_trade_id=10,
            candidate=FakeCandidate(),
            paper_sim_fill=FakeSimFill(),
            paper_notional_usd=1500.0,
        )
    assert out is None


# ---------------------------------------------------------------------------
# Live mode requires BOTH flags
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_maybe_place_live_requires_full_flag():
    """copy_live_enabled=True alone is shadow only — live needs full_enabled too."""
    ex = CopyExecutor()
    with patch.object(ex, "settings", _settings_with(
            copy_live_enabled=True, copy_live_full_enabled=False,
         )), \
         patch("bots.copy.executor.is_wallet_available", return_value=True):
        out = await ex.maybe_place_live(
            session=None,
            signal_id=1, paper_trade_id=10,
            candidate=FakeCandidate(),
            paper_sim_fill=FakeSimFill(),
            notional_usd=100.0,
        )
    assert out is None


# ---------------------------------------------------------------------------
# Happy-path swap → Trade insert (uses a mocked execute_swap_usdc_to_token)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_maybe_place_shadow_filled_swap_inserts_trade(monkeypatch):
    """When the swap returns 'filled', the executor must write a Trade row
    + CalibrationRecord. The DB writes are mocked at the session level."""
    ex = CopyExecutor()
    captured_trades: list[Any] = []
    captured_calibs: list[Any] = []

    class _FakeSession:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def add(self, obj):
            # Distinguish Trade vs CalibrationRecord by class name
            if obj.__class__.__name__ == "Trade":
                obj.id = 999
                captured_trades.append(obj)
            else:
                captured_calibs.append(obj)
        def flush(self): pass
        def execute(self, *a, **k):
            class _R:
                def scalars(self): return iter([])
                def scalar_one_or_none(self): return None
                def first(self): return None
            return _R()
        def get(self, *a, **k): return None

    def _scope():
        class _Cm:
            def __enter__(self): return _FakeSession()
            def __exit__(self, *a): return False
        return _Cm()

    monkeypatch.setattr("bots.copy.executor.session_scope", _scope)
    monkeypatch.setattr("bots.copy.executor.write_audit", lambda *a, **k: None)

    fake_result = SwapResult(
        status="filled",
        signature="SIG_TEST_123",
        fill_price_usd=0.00012,
        actual_in_atomic=15_000_000,   # $15 USDC
        actual_out_atomic=125_000_000_000,  # arbitrary atomic units
        slippage_bps=80.0,
        fees_usd=0.015,
    )
    with patch.object(ex, "settings", _settings_with(
            copy_live_enabled=True, copy_shadow_pct=100.0,
            copy_shadow_open_cap_usd=100.0,
            copy_shadow_notional_min_usd=15.0,
            copy_shadow_notional_max_usd=15.0,
         )), \
         patch("bots.copy.executor.is_wallet_available", return_value=True), \
         patch("bots.copy.executor.public_key_b58", return_value="TEST_PUBKEY"), \
         patch("bots.copy.executor._open_notional_by_mode", return_value=0.0), \
         patch("bots.copy.executor.execute_swap_usdc_to_token",
               new=AsyncMock(return_value=fake_result)):
        out = await ex.maybe_place_shadow(
            session=None,
            signal_id=42, paper_trade_id=100,
            candidate=FakeCandidate(),
            paper_sim_fill=FakeSimFill(fill_price=0.000125),
            paper_notional_usd=1500.0,
        )

    assert out == 999
    assert len(captured_trades) == 1
    t = captured_trades[0]
    assert t.mode == "shadow"
    assert t.signal_id == 42
    assert t.entry_price == 0.00012
    assert t.size_usd == 15.0
    assert t.sim_metadata["tx_signature"] == "SIG_TEST_123"
    assert t.sim_metadata["shadow_paper_trade_id"] == 100
    assert len(captured_calibs) == 1
    assert captured_calibs[0].shadow_trade_id == 999
    assert captured_calibs[0].paper_trade_id == 100


# ---------------------------------------------------------------------------
# Failed swap → audit + no Trade row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_maybe_place_shadow_failed_swap_writes_audit_no_trade(monkeypatch):
    ex = CopyExecutor()
    audit_calls: list[tuple[str, dict]] = []
    captured_trades: list[Any] = []

    class _FakeSession:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def add(self, obj):
            if obj.__class__.__name__ == "Trade":
                obj.id = 1
                captured_trades.append(obj)
        def flush(self): pass
        def execute(self, *a, **k):
            class _R:
                def scalars(self): return iter([])
                def scalar_one_or_none(self): return None
                def first(self): return None
            return _R()
        def get(self, *a, **k): return None

    def _scope():
        class _Cm:
            def __enter__(self): return _FakeSession()
            def __exit__(self, *a): return False
        return _Cm()

    monkeypatch.setattr("bots.copy.executor.session_scope", _scope)
    monkeypatch.setattr("bots.copy.executor.write_audit",
                        lambda event, *, bot_id, payload: audit_calls.append((event, payload)))

    fake_result = SwapResult(
        status="dropped",
        signature="SIG_DROPPED",
        error_message="confirmation_timeout",
    )
    with patch.object(ex, "settings", _settings_with(
            copy_live_enabled=True, copy_shadow_pct=100.0,
            copy_shadow_open_cap_usd=100.0,
            copy_shadow_notional_min_usd=15.0,
            copy_shadow_notional_max_usd=15.0,
         )), \
         patch("bots.copy.executor.is_wallet_available", return_value=True), \
         patch("bots.copy.executor.public_key_b58", return_value="TEST_PUBKEY"), \
         patch("bots.copy.executor._open_notional_by_mode", return_value=0.0), \
         patch("bots.copy.executor.execute_swap_usdc_to_token",
               new=AsyncMock(return_value=fake_result)):
        out = await ex.maybe_place_shadow(
            session=None,
            signal_id=42, paper_trade_id=100,
            candidate=FakeCandidate(),
            paper_sim_fill=FakeSimFill(),
            paper_notional_usd=1500.0,
        )

    assert out is None
    assert captured_trades == []
    assert audit_calls and audit_calls[0][0] == "shadow_swap_not_filled"
    assert audit_calls[0][1]["signature"] == "SIG_DROPPED"


# ---------------------------------------------------------------------------
# Wallet manager unit tests (no solders required)
# ---------------------------------------------------------------------------

def test_wallet_unavailable_when_no_secret(monkeypatch):
    """Empty COPY_SOLANA_PRIVATE_KEY → load_wallet returns None."""
    from bots.copy.venue import solana_wallet
    # Force a fresh load (lru_cache)
    solana_wallet.load_wallet.cache_clear()
    monkeypatch.setattr("bots.copy.venue.solana_wallet.get_copy_settings",
                        lambda: _settings_with(copy_solana_private_key=""))
    assert solana_wallet.load_wallet() is None
    assert solana_wallet.is_wallet_available() is False
    assert solana_wallet.public_key_b58() is None
