"""Tests for SellClusterDetector — the long-side stop signal.

Pure-function tests. No DB, no network. Mirrors the structure of
test_copy_signals.py for the buy ClusterDetector.

The detector must:
- Drop sub-floor sells (< $1k) as wallet rebalancing noise
- Trigger at SELL_CLUSTER_MIN_WALLETS (2) distinct wallets, NOT below
- Use the same wallet as multiple observations → count as 1 distinct wallet
- Honor the rolling window (15 min) — sells older than that get pruned
- Suppress re-fires within the window after one trigger
- Emit signal_type='sell_cluster' direction='exit' with correct payload
"""
from __future__ import annotations

from bots.copy.config import (
    SELL_CLUSTER_MIN_NOTIONAL_PER_WALLET_USD,
    SELL_CLUSTER_MIN_WALLETS,
)
from bots.copy.signals.sell_cluster import SellClusterDetector, WINDOW_SECONDS
from bots.copy.venue.helius_solana import WalletSellEvent


CHAIN = "solana"
TOKEN = "TOKEN_MINT_AAA"
NOW_MS = 1_700_000_000_000


def _ev(wallet: str, notional_usd: float, ts_offset_s: int = 0) -> WalletSellEvent:
    return WalletSellEvent(
        wallet_address=wallet,
        chain=CHAIN,
        token_mint=TOKEN,
        notional_usd=notional_usd,
        timestamp_ms=NOW_MS + ts_offset_s * 1000,
        tx_signature=f"sig-{wallet}-{ts_offset_s}",
    )


def test_no_signal_below_min_wallets():
    """One distinct wallet selling at qualifying notional is not a cluster."""
    d = SellClusterDetector()
    d.observe_sell(_ev("w1", 5000))
    candidates = d.evaluate(now_ms=NOW_MS)
    assert candidates == []


def test_signal_at_two_qualifying_wallets():
    """SELL_CLUSTER_MIN_WALLETS is 2 — lower than buy's 3 by design."""
    assert SELL_CLUSTER_MIN_WALLETS == 2
    d = SellClusterDetector()
    d.observe_sell(_ev("w1", 5000))
    d.observe_sell(_ev("w2", 5000))
    candidates = d.evaluate(now_ms=NOW_MS + 1000)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.signal_type == "sell_cluster"
    assert c.direction == "exit"
    assert c.asset == TOKEN
    assert c.chain == CHAIN
    assert c.cluster_size == 2
    assert c.stop_pct is None      # sell-cluster IS the exit
    assert c.take_profit_pct is None
    assert c.timeout_hours is None
    assert "wallets" in c.payload
    assert set(c.payload["wallets"]) == {"w1", "w2"}


def test_below_notional_floor_dropped():
    """Sub-$1k sells don't count as qualifying — they're noise."""
    d = SellClusterDetector()
    d.observe_sell(_ev("w1", 500))   # below floor
    d.observe_sell(_ev("w2", 500))   # below floor
    d.observe_sell(_ev("w3", 5000))  # above floor, but only 1 qualifying
    candidates = d.evaluate(now_ms=NOW_MS)
    assert candidates == []


def test_same_wallet_multiple_sells_counts_as_one():
    """Two sells by the same wallet ≠ 2-wallet cluster."""
    d = SellClusterDetector()
    d.observe_sell(_ev("w1", 5000, ts_offset_s=0))
    d.observe_sell(_ev("w1", 5000, ts_offset_s=30))
    d.observe_sell(_ev("w1", 5000, ts_offset_s=60))
    candidates = d.evaluate(now_ms=NOW_MS + 90_000)
    assert candidates == []


def test_old_sells_pruned():
    """Sells older than WINDOW_SECONDS get pruned and don't count."""
    d = SellClusterDetector()
    # First sell is well-outside the window relative to NOW eval time
    d.observe_sell(_ev("w1", 5000, ts_offset_s=-(WINDOW_SECONDS + 100)))
    d.observe_sell(_ev("w2", 5000, ts_offset_s=0))
    candidates = d.evaluate(now_ms=NOW_MS + 1000)
    # Only w2 qualifies after pruning → below MIN_WALLETS
    assert candidates == []


def test_refire_suppressed_within_window():
    """After firing once, another event in the same window doesn't re-fire."""
    d = SellClusterDetector()
    d.observe_sell(_ev("w1", 5000, ts_offset_s=0))
    d.observe_sell(_ev("w2", 5000, ts_offset_s=0))
    first = d.evaluate(now_ms=NOW_MS + 1000)
    assert len(first) == 1

    # Add a third wallet a minute later
    d.observe_sell(_ev("w3", 5000, ts_offset_s=60))
    second = d.evaluate(now_ms=NOW_MS + 61_000)
    assert second == []


def test_refire_allowed_after_window():
    """Past the WINDOW_SECONDS suppression, a new cluster on the same token
    can fire again. (Downstream cluster_detections dedup also gates at a
    longer horizon; this is the in-memory layer.)"""
    d = SellClusterDetector()
    d.observe_sell(_ev("w1", 5000, ts_offset_s=0))
    d.observe_sell(_ev("w2", 5000, ts_offset_s=0))
    first = d.evaluate(now_ms=NOW_MS + 1000)
    assert len(first) == 1

    # Add 2 new wallets selling well past the window — old sells get pruned,
    # suppression check is also outside the window
    future_offset = WINDOW_SECONDS + 100
    d.observe_sell(_ev("w3", 5000, ts_offset_s=future_offset))
    d.observe_sell(_ev("w4", 5000, ts_offset_s=future_offset))
    second = d.evaluate(now_ms=NOW_MS + (future_offset + 1) * 1000)
    assert len(second) == 1
    assert set(second[0].payload["wallets"]) == {"w3", "w4"}


def test_payload_includes_per_wallet_notionals():
    """Downstream attribution wants per-wallet notionals to assess
    sell-cluster confidence (small sells from many wallets ≠ big sells
    from a few)."""
    d = SellClusterDetector()
    d.observe_sell(_ev("w1", 1500))
    d.observe_sell(_ev("w2", 8000))
    candidates = d.evaluate(now_ms=NOW_MS + 1000)
    assert len(candidates) == 1
    notionals = candidates[0].payload["wallet_notionals"]
    assert notionals["w1"] == 1500
    assert notionals["w2"] == 8000
    assert candidates[0].payload["total_notional_usd"] == 9500
    assert candidates[0].payload["avg_notional_usd"] == 4750


def test_independent_tokens_dont_cross():
    """Sells on token A don't count toward a sell-cluster on token B."""
    d = SellClusterDetector()
    d.observe_sell(WalletSellEvent(
        wallet_address="w1", chain=CHAIN, token_mint="TOKEN_A",
        notional_usd=5000, timestamp_ms=NOW_MS, tx_signature="sig-1",
    ))
    d.observe_sell(WalletSellEvent(
        wallet_address="w2", chain=CHAIN, token_mint="TOKEN_B",
        notional_usd=5000, timestamp_ms=NOW_MS, tx_signature="sig-2",
    ))
    candidates = d.evaluate(now_ms=NOW_MS + 1000)
    assert candidates == []


def test_notional_floor_matches_config():
    """The detector's threshold must match the locked config constant."""
    assert SELL_CLUSTER_MIN_NOTIONAL_PER_WALLET_USD == 1_000.0
