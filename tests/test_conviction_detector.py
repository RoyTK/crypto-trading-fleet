"""Tests for the COPY conviction (single-wallet trigger) strategy.

Three layers, all DB-free:
  1. ConvictionDetector — pure stateful detector (allowlist gate, notional
     floor, re-fire suppression, candidate shape).
  2. size_conviction_position — pure sizing.
  3. _compute_copy_conviction_status — threshold logic with a mocked session
     (mirrors test_kill_criteria_monitor's mock approach).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bots.copy.signals.conviction import ConvictionDetector, SUPPRESS_SECONDS
from bots.copy.sizing import size_conviction_position
from bots.copy.venue.helius_solana import WalletBuyEvent
from framework import kill_criteria_monitor as kcm


ROSTER = "EliteWallet1111111111111111111111111111111"
OTHER = "RandomWallet22222222222222222222222222222222"
TOKEN = "Tok1111111111111111111111111111111111111111"
FLOOR = 1_000.0


def _buy(wallet=ROSTER, token=TOKEN, notional=5_000.0, ts_ms=1_000_000):
    return WalletBuyEvent(
        wallet_address=wallet,
        chain="solana",
        token_mint=token,
        notional_usd=notional,
        timestamp_ms=ts_ms,
        tx_signature="sig",
    )


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

def test_non_roster_wallet_never_fires():
    d = ConvictionDetector(wallets={ROSTER}, min_notional_usd=FLOOR)
    d.observe_buy(_buy(wallet=OTHER))
    assert d.evaluate(now_ms=2_000_000) == []


def test_below_notional_floor_ignored():
    d = ConvictionDetector(wallets={ROSTER}, min_notional_usd=FLOOR)
    d.observe_buy(_buy(notional=999.0))
    assert d.evaluate(now_ms=2_000_000) == []


def test_roster_buy_above_floor_fires_conviction_candidate():
    d = ConvictionDetector(wallets={ROSTER}, min_notional_usd=FLOOR)
    d.observe_buy(_buy(notional=5_000.0))
    out = d.evaluate(now_ms=2_000_000)
    assert len(out) == 1
    c = out[0]
    assert c.signal_type == "conviction_buy"
    assert c.asset == TOKEN
    assert c.chain == "solana"
    assert c.direction == "long"
    assert c.cluster_size == 1
    assert c.payload["strategy"] == "conviction"
    assert c.payload["trigger_wallet"] == ROSTER
    assert c.payload["wallets"] == [ROSTER]


def test_refire_suppressed_within_window():
    d = ConvictionDetector(wallets={ROSTER}, min_notional_usd=FLOOR)
    d.observe_buy(_buy(ts_ms=1_000_000))
    first = d.evaluate(now_ms=1_000_000)
    assert len(first) == 1
    # A second buy of the SAME token shortly after must be suppressed.
    d.observe_buy(_buy(ts_ms=1_100_000))
    within = d.evaluate(now_ms=1_000_000 + (SUPPRESS_SECONDS * 1000) - 1)
    assert within == []


def test_refire_allowed_after_window():
    d = ConvictionDetector(wallets={ROSTER}, min_notional_usd=FLOOR)
    d.observe_buy(_buy(ts_ms=1_000_000))
    d.evaluate(now_ms=1_000_000)
    later = 1_000_000 + (SUPPRESS_SECONDS * 1000) + 1
    d.observe_buy(_buy(ts_ms=later))
    out = d.evaluate(now_ms=later)
    assert len(out) == 1


def test_set_wallets_refreshes_roster():
    d = ConvictionDetector(wallets=set(), min_notional_usd=FLOOR)
    d.observe_buy(_buy())
    assert d.evaluate(now_ms=2_000_000) == []  # empty roster
    d.set_wallets({ROSTER})
    d.observe_buy(_buy(ts_ms=3_000_000))
    assert len(d.evaluate(now_ms=3_000_000)) == 1


def test_distinct_tokens_fire_independently():
    d = ConvictionDetector(wallets={ROSTER}, min_notional_usd=FLOOR)
    d.observe_buy(_buy(token="AAAA", ts_ms=1_000_000))
    d.observe_buy(_buy(token="BBBB", ts_ms=1_000_000))
    out = d.evaluate(now_ms=1_000_000)
    assert {c.asset for c in out} == {"AAAA", "BBBB"}


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

CAP = 10_000.0


def test_conviction_size_is_4pct_of_bankroll():
    notional = size_conviction_position(paper_capital_usd=CAP)
    assert notional == pytest.approx(CAP * 0.04, rel=1e-6)


def test_conviction_alloc_cap_shrinks_to_headroom():
    # 48% already open → only 2% headroom before the 50% cap
    notional = size_conviction_position(paper_capital_usd=CAP, current_open_alloc_pct=48.0)
    assert notional == pytest.approx(CAP * 0.02, rel=1e-6)


def test_conviction_alloc_cap_full_returns_zero():
    notional = size_conviction_position(paper_capital_usd=CAP, current_open_alloc_pct=50.0)
    assert notional == 0.0


def test_conviction_dd_discount_at_halt_floor():
    base = size_conviction_position(paper_capital_usd=CAP, current_dd_today_pct=0.0)
    halved = size_conviction_position(paper_capital_usd=CAP, current_dd_today_pct=12.0)
    assert halved == pytest.approx(base * 0.5, rel=1e-6)


# ---------------------------------------------------------------------------
# Kill-criteria computer (mocked session)
# ---------------------------------------------------------------------------

def _mock_session(pnls):
    session_cm = MagicMock()
    session = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False

    def execute_side_effect(sql, *args, **kwargs):
        result = MagicMock()
        result.all.return_value = [MagicMock(pnl_usd=p) for p in pnls]
        result.first.return_value = None
        return result

    session.execute.side_effect = execute_side_effect
    return session_cm


def test_conviction_status_empty_no_triggers():
    with patch.object(kcm, "session_scope", return_value=_mock_session([])), \
         patch.object(kcm, "_paper_capital_for", return_value=10_000.0):
        status = kcm._compute_copy_conviction_status()
    assert status["bot_id"] == "copy_conviction"
    assert status["strategy"] == "conviction"
    assert status["n"] == 0
    assert status["kill_triggers"] == []
    assert status["promote_eligible"] is False


def test_conviction_wr_below_floor_fires_at_n60():
    # 60 trades, 10 winners → WR 0.167 < 0.25 floor; large losses → pnl also below floor
    pnls = [5.0] * 10 + [-10.0] * 50
    with patch.object(kcm, "session_scope", return_value=_mock_session(pnls)), \
         patch.object(kcm, "_paper_capital_for", return_value=10_000.0):
        status = kcm._compute_copy_conviction_status()
    assert status["n"] == 60
    assert "copy_conviction_wr_below_floor" in status["kill_triggers"]


def test_conviction_n_below_gate_skips_evaluation():
    pnls = [-1.0] * 40  # WR 0 but n<60
    with patch.object(kcm, "session_scope", return_value=_mock_session(pnls)), \
         patch.object(kcm, "_paper_capital_for", return_value=10_000.0):
        status = kcm._compute_copy_conviction_status()
    assert status["n"] == 40
    assert status["kill_triggers"] == []


def test_conviction_promote_eligible_when_strong():
    # 60 trades, strong WR + big positive PnL + low variance for a real Sharpe
    pnls = [40.0] * 40 + [-10.0] * 20  # WR 0.667, net +$1400 = +14% on $10k
    with patch.object(kcm, "session_scope", return_value=_mock_session(pnls)), \
         patch.object(kcm, "_paper_capital_for", return_value=10_000.0):
        status = kcm._compute_copy_conviction_status()
    assert status["n"] == 60
    assert status["wr"] >= 0.55
    assert status["net_pnl_pct"] >= 5.0
    assert status["promote_eligible"] is True
