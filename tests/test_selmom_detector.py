"""Selector-momentum detector: queues one candidate per token on a selector buy, ignores others."""
from bots.copy.signals.selmom import SelectorMomentumDetector
from bots.copy.venue.helius_solana import WalletBuyEvent


def _ev(wallet, token, usd=100.0, chain="solana"):
    return WalletBuyEvent(wallet_address=wallet, chain=chain, token_mint=token,
                          notional_usd=usd, timestamp_ms=1_000, tx_signature="sig")


def test_selector_buy_queues_candidate_with_fleet_exits():
    d = SelectorMomentumDetector(["SEL"])
    d.observe_buy(_ev("SEL", "TOK"))
    out = d.evaluate()
    assert len(out) == 1
    c = out[0]
    assert c.signal_type == "selmom_buy" and c.asset == "TOK"
    assert c.payload["trigger_wallet"] == "SEL"
    assert c.stop_pct == 8.0 and c.timeout_hours == 12
    assert d.evaluate() == []


def test_non_selector_and_repeat_token_ignored():
    d = SelectorMomentumDetector(["SEL"])
    d.observe_buy(_ev("OTHER", "TOK"))
    d.observe_buy(_ev("SEL", "TOK"))
    d.observe_buy(_ev("SEL", "TOK"))
    d.observe_buy(_ev("SEL", "TOK2", chain="base"))
    assert [c.asset for c in d.evaluate()] == ["TOK"]
