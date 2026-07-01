"""Tests for the COPY team-follow detector (isolated 'teamfollow' strategy).

TeamFollowDetector fires when >= min_members distinct members of the SAME team
co-buy a token within the window. DB-free / pure. Mirrors the cluster co-buy
mechanics but team-scoped.
"""
from __future__ import annotations

from bots.copy.signals.teamfollow import TeamFollowDetector
from bots.copy.venue.helius_solana import WalletBuyEvent

# Team 1 = {A, B}; Team 2 = {C, D}; Z = non-roster
A = "AwalletA1111111111111111111111111111111111"
B = "BwalletB2222222222222222222222222222222222"
C = "CwalletC3333333333333333333333333333333333"
D = "DwalletD4444444444444444444444444444444444"
Z = "ZwalletZ9999999999999999999999999999999999"
TOKEN = "Tok1111111111111111111111111111111111111111"
ROSTER = {A: 1, B: 1, C: 2, D: 2}

WIN_MIN = 60
WIN_MS = WIN_MIN * 60 * 1000
T0 = 1_000_000


def _det(**over):
    kw = dict(roster=ROSTER, min_members=2, window_minutes=WIN_MIN, dust_floor_usd=50.0)
    kw.update(over)
    return TeamFollowDetector(**kw)


def _buy(wallet=A, token=TOKEN, notional=100.0, ts_ms=T0):
    return WalletBuyEvent(
        wallet_address=wallet, chain="solana", token_mint=token,
        notional_usd=notional, timestamp_ms=ts_ms, tx_signature="sig",
    )


def test_two_same_team_members_fire():
    d = _det()
    d.observe_buy(_buy(A, ts_ms=T0))
    d.observe_buy(_buy(B, ts_ms=T0 + 5000))
    out = d.evaluate(now_ms=T0 + 6000)
    assert len(out) == 1
    c = out[0]
    assert c.signal_type == "teamfollow_buy"
    assert c.payload["strategy"] == "teamfollow"
    assert c.payload["team_id"] == 1
    assert set(c.payload["wallets"]) == {A, B}
    assert c.cluster_size == 2


def test_two_different_team_members_do_not_fire():
    d = _det()
    d.observe_buy(_buy(A, ts_ms=T0))          # team 1
    d.observe_buy(_buy(C, ts_ms=T0 + 5000))   # team 2
    assert d.evaluate(now_ms=T0 + 6000) == []


def test_single_member_does_not_fire():
    d = _det()
    d.observe_buy(_buy(A, ts_ms=T0))
    assert d.evaluate(now_ms=T0 + 1000) == []


def test_non_roster_wallet_ignored():
    d = _det()
    d.observe_buy(_buy(A, ts_ms=T0))
    d.observe_buy(_buy(Z, ts_ms=T0 + 5000))   # not in roster
    assert d.evaluate(now_ms=T0 + 6000) == []


def test_dust_buys_ignored():
    d = _det()
    d.observe_buy(_buy(A, notional=10.0, ts_ms=T0))       # below dust floor
    d.observe_buy(_buy(B, notional=100.0, ts_ms=T0 + 5000))
    assert d.evaluate(now_ms=T0 + 6000) == []


def test_window_pruning_drops_old_buy():
    d = _det()
    d.observe_buy(_buy(A, ts_ms=T0))
    # B buys > window later — A's buy has aged out, so no co-buy
    d.observe_buy(_buy(B, ts_ms=T0 + WIN_MS + 1000))
    assert d.evaluate(now_ms=T0 + WIN_MS + 2000) == []


def test_refire_suppressed_within_window_then_fires_after():
    d = _det()
    d.observe_buy(_buy(A, ts_ms=T0))
    d.observe_buy(_buy(B, ts_ms=T0 + 5000))
    assert len(d.evaluate(now_ms=T0 + 6000)) == 1
    # another co-buy shortly after — suppressed (same team+token, window active)
    d.observe_buy(_buy(A, ts_ms=T0 + 7000))
    d.observe_buy(_buy(B, ts_ms=T0 + 8000))
    assert d.evaluate(now_ms=T0 + 9000) == []
    # after the window elapses, a fresh co-buy fires again
    t2 = T0 + 2 * WIN_MS
    d.observe_buy(_buy(A, ts_ms=t2))
    d.observe_buy(_buy(B, ts_ms=t2 + 5000))
    assert len(d.evaluate(now_ms=t2 + 6000)) == 1


def test_three_member_team_reports_cluster_size():
    roster = {A: 1, B: 1, C: 1}   # 3-member team
    d = _det(roster=roster)
    d.observe_buy(_buy(A, ts_ms=T0))
    d.observe_buy(_buy(B, ts_ms=T0 + 1000))
    d.observe_buy(_buy(C, ts_ms=T0 + 2000))
    out = d.evaluate(now_ms=T0 + 3000)
    assert len(out) == 1
    assert out[0].cluster_size == 3
    assert set(out[0].payload["wallets"]) == {A, B, C}
