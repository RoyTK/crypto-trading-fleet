"""Find diamond-hand accumulators of a runner: wallets that bought EARLY and still HOLD.

The conviction-wallet sourcing primitive (Ge87E proof-of-concept, 2026-07-19). For a
given token mint: pull the earliest N buyers (Birdeye first-buyers, paginated) ∩ the
current top holders → wallets that bought at/near launch and are STILL holding a
meaningful bag = the accumulators conviction wants to follow (vs the snipers/traders
that trigger us today). Cross-refs our wallet_pool so only UNKNOWN candidates surface,
and separates clean `smart_trader` tags from `bundler` (coordinated launch ring / team
allocation = observe-only per the legal line, NOT a skill signal).

Usage (in bot_copy):  docker compose exec -T bot_copy python -m scripts.find_held_accumulators <MINT> [min_value_usd]
Read-only. Industrialize by looping this over the runner corpus (prerun_scans) and
tallying wallets that recur across MANY runners = the repeat accumulators.
"""
import json
import sys
import time
import urllib.request

from sqlalchemy import text
from framework.db import session_scope
from bots.copy.config import get_copy_settings

_K = get_copy_settings().birdeye_api_key


def _be(path):
    r = urllib.request.Request(
        "https://public-api.birdeye.so" + path,
        headers={"X-API-KEY": _K, "x-chain": "solana", "accept": "application/json",
                 "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(r, timeout=25) as resp:
        return json.loads(resp.read().decode())


def held_accumulators(mint: str, n_early: int = 500, min_value_usd: float = 1000.0):
    """Return list of dicts: early buyers still holding >= min_value_usd, richest first."""
    price = float((_be("/defi/price?address=" + mint).get("data") or {}).get("value") or 0)
    first = {}
    for off in range(0, n_early, 100):
        fb = _be(f"/token/v1/first-buyers?token_address={mint}&limit=100&offset={off}")
        buyers = (fb.get("data") or {}).get("buyers") or []
        for j, b in enumerate(buyers):
            w = b.get("wallet_address")
            if w and w not in first:
                first[w] = {"rank": off + j + 1,
                            "launch_usd": float(b.get("first_buy_volume_usd") or 0),
                            "tags": b.get("tags") or []}
        time.sleep(0.35)
        if len(buyers) < 100:
            break
    hold = {}
    for off in (0, 100):
        d = _be(f"/defi/v3/token/holder?address={mint}&offset={off}&limit=100")
        for h in (d.get("data") or {}).get("items") or []:
            w = h.get("owner") or h.get("address")
            if w:
                hold[w] = float(h.get("ui_amount") or h.get("uiAmount") or 0)
        time.sleep(0.35)
    with session_scope() as s:
        pool = {a for (a,) in s.execute(text("SELECT address FROM wallet_pool")).all()}
    out = []
    for w, meta in first.items():
        val = hold.get(w, 0) * price
        if val >= min_value_usd:
            tags = meta["tags"]
            out.append({"wallet": w, "value_usd": round(val), "rank": meta["rank"],
                        "launch_usd": round(meta["launch_usd"]), "tags": tags,
                        "clean_smart": ("smart_trader" in tags and "bundler" not in tags),
                        "in_pool": w in pool})
    out.sort(key=lambda x: -x["value_usd"])
    return out, price


def main():
    if len(sys.argv) < 2:
        print("usage: python -m scripts.find_held_accumulators <MINT> [min_value_usd]")
        return
    mint = sys.argv[1]
    minv = float(sys.argv[2]) if len(sys.argv) > 2 else 1000.0
    rows, price = held_accumulators(mint, min_value_usd=minv)
    print(f"{mint}  price ${price:.6f}  |  early-and-holding >= ${minv:,.0f}: {len(rows)}")
    print(f"{'wallet':<12} {'value$':>10} {'rank':>5} {'launch$':>8}  clean  pool  tags")
    for r in rows:
        print(f"{r['wallet'][:12]:<12} {r['value_usd']:>10,} {r['rank']:>5} {r['launch_usd']:>8,}  "
              f"{'YES' if r['clean_smart'] else '  -'}   {'Y' if r['in_pool'] else 'N'}   {','.join(r['tags']) or '-'}")


if __name__ == "__main__":
    main()
