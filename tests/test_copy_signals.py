"""Pure-function tests for COPY's cluster detector. No DB, no network."""
import pytest

from bots.copy.config import CLUSTER_MIN_NOTIONAL_PER_WALLET_USD
from bots.copy.signals.cluster import ClusterDetector, TokenMeta
from bots.copy.venue.helius_solana import WalletBuyEvent


CHAIN = "solana"
TOKEN = "TOKEN_MINT_AAA"
NOW_MS = 1_700_000_000_000


def _ev(wallet: str, notional_usd: float, ts_offset_s: int = 0) -> WalletBuyEvent:
    return WalletBuyEvent(
        wallet_address=wallet,
        chain=CHAIN,
        token_mint=TOKEN,
        notional_usd=notional_usd,
        timestamp_ms=NOW_MS + ts_offset_s * 1000,
        tx_signature=f"sig-{wallet}-{ts_offset_s}",
    )


def test_no_signal_below_min_wallets():
    d = ClusterDetector()
    d.observe_buy(_ev("w1", 6000))
    d.observe_buy(_ev("w2", 6000))
    candidates = d.evaluate(now_ms=NOW_MS)
    assert candidates == []


def test_signal_at_three_qualifying_wallets():
    d = ClusterDetector()
    d.observe_buy(_ev("w1", 6000))
    d.observe_buy(_ev("w2", 6000))
    d.observe_buy(_ev("w3", 6000))
    candidates = d.evaluate(now_ms=NOW_MS + 1000)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.signal_type == "cluster_buy"
    assert c.asset == TOKEN
    assert c.chain == CHAIN
    assert c.direction == "long"
    assert c.cluster_size == 3
    assert c.payload["total_notional_usd"] == pytest.approx(18000)


def test_below_per_wallet_floor_does_not_count():
    d = ClusterDetector()
    d.observe_buy(_ev("w1", 6000))
    d.observe_buy(_ev("w2", 6000))
    # w3 spent only $1k — should not qualify
    d.observe_buy(_ev("w3", 1000))
    candidates = d.evaluate(now_ms=NOW_MS)
    assert candidates == []


def test_distinct_wallets_required():
    d = ClusterDetector()
    # Same wallet 3 times — counts as 1 distinct wallet
    d.observe_buy(_ev("w1", 6000, ts_offset_s=0))
    d.observe_buy(_ev("w1", 6000, ts_offset_s=10))
    d.observe_buy(_ev("w1", 6000, ts_offset_s=20))
    candidates = d.evaluate(now_ms=NOW_MS + 30000)
    assert candidates == []


def test_window_pruning_drops_old_events():
    d = ClusterDetector()
    # Two old events (>15min ago) + one recent = no cluster
    d.observe_buy(_ev("w1", 6000, ts_offset_s=-20 * 60))  # 20 min ago
    d.observe_buy(_ev("w2", 6000, ts_offset_s=-20 * 60))
    d.observe_buy(_ev("w3", 6000, ts_offset_s=0))
    candidates = d.evaluate(now_ms=NOW_MS)
    assert candidates == []


def test_token_meta_passes_when_age_under_24h():
    d = ClusterDetector()
    d.observe_buy(_ev("w1", 6000))
    d.observe_buy(_ev("w2", 6000))
    d.observe_buy(_ev("w3", 6000))
    meta = {(CHAIN, TOKEN): TokenMeta(age_hours=2.0)}
    candidates = d.evaluate(token_meta=meta, now_ms=NOW_MS + 1000)
    assert len(candidates) == 1


def test_token_meta_blocks_old_token_with_no_vol_jump():
    d = ClusterDetector()
    d.observe_buy(_ev("w1", 6000))
    d.observe_buy(_ev("w2", 6000))
    d.observe_buy(_ev("w3", 6000))
    meta = {(CHAIN, TOKEN): TokenMeta(
        age_hours=72.0,
        last_hour_vol_usd=100_000,
        prior_24h_avg_hourly_vol_usd=100_000,  # 1.0× — not 5×
    )}
    candidates = d.evaluate(token_meta=meta, now_ms=NOW_MS + 1000)
    assert candidates == []


def test_token_meta_passes_old_token_with_vol_jump():
    d = ClusterDetector()
    d.observe_buy(_ev("w1", 6000))
    d.observe_buy(_ev("w2", 6000))
    d.observe_buy(_ev("w3", 6000))
    meta = {(CHAIN, TOKEN): TokenMeta(
        age_hours=72.0,
        last_hour_vol_usd=600_000,
        prior_24h_avg_hourly_vol_usd=100_000,  # 6× jump — passes
    )}
    candidates = d.evaluate(token_meta=meta, now_ms=NOW_MS + 1000)
    assert len(candidates) == 1


def test_no_refire_within_window():
    d = ClusterDetector()
    d.observe_buy(_ev("w1", 6000))
    d.observe_buy(_ev("w2", 6000))
    d.observe_buy(_ev("w3", 6000))
    first = d.evaluate(now_ms=NOW_MS)
    assert len(first) == 1
    # 5 min later, more buys — should NOT re-fire
    d.observe_buy(_ev("w4", 6000, ts_offset_s=300))
    second = d.evaluate(now_ms=NOW_MS + 5 * 60 * 1000)
    assert second == []


def test_below_per_wallet_floor_constant_check():
    """Sanity that the threshold is what we expect."""
    assert CLUSTER_MIN_NOTIONAL_PER_WALLET_USD == 5000.0
