"""Balance-change fallback parser (2026-11-17): multi-hop TRANSFER-typed trades with a dust reward token."""
from monitoring.webhook_receiver.main import _parse_swap_event, _parse_sell_event

W = "WALLET111111111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"


def _ev(native, toks):
    return {"timestamp": 1_800_000_000, "signature": "sig", "type": "TRANSFER",
            "accountData": [{"account": W, "nativeBalanceChange": native, "tokenBalanceChanges": [
                {"userAccount": W, "mint": m, "rawTokenAmount": {"tokenAmount": str(a), "decimals": d}}
                for m, a, d in toks]}]}


def test_multihop_buy_with_reward_dust_picks_main_token():
    e = _ev(-265_408_967, [("MEME", 2038165785564, 6), ("REWARD", 976, 6)])
    b = _parse_swap_event(e, {W}, 180.0)
    assert b and b["token_mint"] == "MEME" and round(b["notional_usd"], 1) == 47.8


def test_multihop_sell():
    e = _ev(378_809_716, [("MEME", -6616219082331, 6), ("REWARD", 2000, 6)])
    assert _parse_swap_event(e, {W}, 180.0) is None
    s = _parse_sell_event(e, {W}, 180.0)
    assert s and s["token_mint"] == "MEME" and s["notional_usd"] > 60


def test_plain_token_transfer_is_not_a_trade():
    e = _ev(-5000, [("MEME", 1000000, 6)])          # only a fee paid, token arrives by transfer
    assert _parse_swap_event(e, {W}, 180.0) is None
    assert _parse_sell_event(e, {W}, 180.0) is None
