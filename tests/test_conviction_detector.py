"""Tests for the COPY conviction (single-wallet accumulation) strategy.

Three layers, all DB-free:
  1. ConvictionDetector — windowed per-(wallet,token) buy accumulation with a
     dust floor, a cumulative threshold, and a sell hold-off.
  2. size_conviction_position — pure sizing.
  3. _compute_copy_conviction_status — threshold logic with a mocked session.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bots.copy.signals.conviction import ConvictionDetector
from bots.copy.sizing import size_conviction_position
from bots.copy.venue.helius_solana import WalletBuyEvent, WalletSellEvent
from framework import kill_criteria_monitor as kcm


ROSTER = "EliteWallet1111111111111111111111111111111"
OTHER = "RandomWallet22222222222222222222222222222222"
TOKEN = "Tok1111111111111111111111111111111111111111"

WIN_MIN = 60
WIN_MS = WIN_MIN * 60 * 1000
T0 = 1_000_000  # base timestamp (ms)


def _det(**over):
    kw = dict(
        wallets={ROSTER},
        dust_floor_usd=10.0,
        threshold_usd=200.0,
        window_minutes=WIN_MIN,
        sell_holdoff_usd=0.0,
    )
    kw.update(over)
    return ConvictionDetector(**kw)


def _buy(wallet=ROSTER, token=TOKEN, notional=16.0, ts_ms=T0):
    return WalletBuyEvent(
        wallet_address=wallet, chain="solana", token_mint=token,
        notional_usd=notional, timestamp_ms=ts_ms, tx_signature="sig",
    )


def _sell(wallet=ROSTER, token=TOKEN, notional=30.0, ts_ms=T0):
    return WalletSellEvent(
        wallet_address=wallet, chain="solana", token_mint=token,
        notional_usd=notional, timestamp_ms=ts_ms, tx_signature="sig",
    )


# ---------------------------------------------------------------------------
# Detector — accumulation
# ---------------------------------------------------------------------------

def test_non_roster_wallet_never_fires():
    d = _det()
    for i in range(20):
        d.observe_buy(_buy(wallet=OTHER, notional=50.0, ts_ms=T0 + i))
    assert d.evaluate(now_ms=T0 + 1000) == []


def test_sub_dust_buys_ignored():
    d = _det()
    for i in range(100):
        d.observe_buy(_buy(notional=5.0, ts_ms=T0 + i))  # $5 < $10 dust
    assert d.evaluate(now_ms=T0 + 1000) == []


def test_single_buy_above_threshold_fires_instantly():
    d = _det()
    d.observe_buy(_buy(notional=250.0, ts_ms=T0))
    out = d.evaluate(now_ms=T0 + 1)
    assert len(out) == 1
    c = out[0]
    assert c.signal_type == "conviction_buy"
    assert c.asset == TOKEN
    assert c.direction == "long"
    assert c.cluster_size == 1
    assert c.payload["strategy"] == "conviction"
    assert c.payload["trigger_wallet"] == ROSTER
    assert c.payload["accumulated_usd"] == pytest.approx(250.0)
    assert c.payload["n_buys"] == 1
    assert c.payload["wallets"] == [ROSTER]


def test_small_buys_accumulate_to_threshold_then_fire():
    d = _det()
    for i in range(13):  # 13 x $16 = $208 >= 200
        d.observe_buy(_buy(notional=16.0, ts_ms=T0 + i * 1000))
    out = d.evaluate(now_ms=T0 + 14_000)
    assert len(out) == 1
    assert out[0].payload["accumulated_usd"] == pytest.approx(208.0)
    assert out[0].payload["n_buys"] == 13


def test_sub_threshold_accumulation_does_not_fire():
    d = _det()
    for i in range(10):  # 10 x $16 = $160 < 200
        d.observe_buy(_buy(notional=16.0, ts_ms=T0 + i * 1000))
    assert d.evaluate(now_ms=T0 + 11_000) == []


def test_window_pruning_drops_old_buys():
    d = _det()
    for i in range(13):  # $208 of buys, all at ~T0
        d.observe_buy(_buy(notional=16.0, ts_ms=T0 + i))
    # Evaluate well after the window — every buy has aged out.
    assert d.evaluate(now_ms=T0 + WIN_MS + 1) == []


def test_fire_resets_accumulation():
    d = _det()
    for i in range(13):
        d.observe_buy(_buy(notional=16.0, ts_ms=T0 + i))
    assert len(d.evaluate(now_ms=T0 + 1000)) == 1
    # Immediately re-evaluating must NOT re-fire — the window was reset on fire.
    assert d.evaluate(now_ms=T0 + 2000) == []


def test_two_wallets_same_token_are_independent():
    d = ConvictionDetector(
        wallets={ROSTER, OTHER}, dust_floor_usd=10.0, threshold_usd=200.0,
        window_minutes=WIN_MIN, sell_holdoff_usd=0.0,
    )
    d.observe_buy(_buy(wallet=ROSTER, notional=250.0, ts_ms=T0))
    d.observe_buy(_buy(wallet=OTHER, notional=120.0, ts_ms=T0))  # below threshold
    out = d.evaluate(now_ms=T0 + 1000)
    assert len(out) == 1
    assert out[0].payload["trigger_wallet"] == ROSTER


def test_set_wallets_refresh():
    d = ConvictionDetector(wallets=set(), dust_floor_usd=10.0, threshold_usd=200.0,
                           window_minutes=WIN_MIN, sell_holdoff_usd=0.0)
    d.observe_buy(_buy(notional=250.0, ts_ms=T0))
    assert d.evaluate(now_ms=T0 + 1000) == []  # empty roster
    d.set_wallets({ROSTER})
    d.observe_buy(_buy(notional=250.0, ts_ms=T0 + 2000))
    assert len(d.evaluate(now_ms=T0 + 3000)) == 1


# ---------------------------------------------------------------------------
# Detector — sell hold-off
# ---------------------------------------------------------------------------

def test_sell_in_window_holds_off_the_buy():
    d = _det()  # sell_holdoff_usd=0 → any non-dust sell suppresses
    d.observe_buy(_buy(notional=250.0, ts_ms=T0))
    d.observe_sell(_sell(notional=30.0, ts_ms=T0 + 500))
    assert d.evaluate(now_ms=T0 + 1000) == []


def test_sub_dust_sell_does_not_hold_off():
    d = _det()
    d.observe_buy(_buy(notional=250.0, ts_ms=T0))
    d.observe_sell(_sell(notional=5.0, ts_ms=T0 + 500))  # $5 < dust → ignored
    assert len(d.evaluate(now_ms=T0 + 1000)) == 1


def test_sell_holdoff_tolerance_allows_small_sells():
    d = _det(sell_holdoff_usd=50.0)
    d.observe_buy(_buy(notional=250.0, ts_ms=T0))
    d.observe_sell(_sell(notional=30.0, ts_ms=T0 + 500))  # 30 <= 50 tolerance
    out = d.evaluate(now_ms=T0 + 1000)
    assert len(out) == 1
    assert out[0].payload["window_sells_usd"] == pytest.approx(30.0)
    # A sell above the tolerance suppresses.
    d2 = _det(sell_holdoff_usd=50.0)
    d2.observe_buy(_buy(notional=250.0, ts_ms=T0))
    d2.observe_sell(_sell(notional=60.0, ts_ms=T0 + 500))
    assert d2.evaluate(now_ms=T0 + 1000) == []


def test_aged_out_sell_no_longer_blocks():
    d = _det()
    d.observe_sell(_sell(notional=30.0, ts_ms=T0))            # old sell
    d.observe_buy(_buy(notional=250.0, ts_ms=T0 + WIN_MS - 1))  # fresh buy
    # Evaluate when the sell has aged out of the window but the buy hasn't.
    out = d.evaluate(now_ms=T0 + WIN_MS + 100)
    assert len(out) == 1


def test_sold_usd_since_tracks_post_trigger_flip():
    """sold_usd_since sums the wallet's non-dust sells at/after a cutoff — the
    entry persistence gate uses it to abort if a whale flips out during the wait."""
    d = _det()
    d.observe_sell(_sell(notional=30.0, ts_ms=T0))          # before cutoff
    d.observe_sell(_sell(notional=50.0, ts_ms=T0 + 1000))   # after cutoff
    d.observe_sell(_sell(notional=5.0, ts_ms=T0 + 2000))    # sub-dust → not recorded
    cutoff = T0 + 500
    assert d.sold_usd_since("solana", TOKEN, ROSTER, cutoff) == 50.0
    assert d.sold_usd_since("solana", TOKEN, ROSTER, T0) == 80.0
    assert d.sold_usd_since("solana", TOKEN, OTHER, T0) == 0.0  # unknown key


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

CAP = 10_000.0


def test_conviction_size_is_4pct_of_bankroll():
    assert size_conviction_position(paper_capital_usd=CAP) == pytest.approx(CAP * 0.04, rel=1e-6)


def test_conviction_alloc_cap_shrinks_to_headroom():
    notional = size_conviction_position(paper_capital_usd=CAP, current_open_alloc_pct=48.0)
    assert notional == pytest.approx(CAP * 0.02, rel=1e-6)


def test_conviction_alloc_cap_full_returns_zero():
    assert size_conviction_position(paper_capital_usd=CAP, current_open_alloc_pct=50.0) == 0.0


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
    assert status["n"] == 0
    assert status["kill_triggers"] == []
    assert status["promote_eligible"] is False


def test_conviction_wr_below_floor_fires_at_n60():
    pnls = [5.0] * 10 + [-10.0] * 50
    with patch.object(kcm, "session_scope", return_value=_mock_session(pnls)), \
         patch.object(kcm, "_paper_capital_for", return_value=10_000.0):
        status = kcm._compute_copy_conviction_status()
    assert status["n"] == 60
    assert "copy_conviction_wr_below_floor" in status["kill_triggers"]


def test_conviction_promote_eligible_when_strong():
    pnls = [40.0] * 40 + [-10.0] * 20
    with patch.object(kcm, "session_scope", return_value=_mock_session(pnls)), \
         patch.object(kcm, "_paper_capital_for", return_value=10_000.0):
        status = kcm._compute_copy_conviction_status()
    assert status["n"] == 60
    assert status["promote_eligible"] is True
