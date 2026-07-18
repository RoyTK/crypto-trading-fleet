"""Does conviction exit too early? (Roy 2026-07-18, the Ge87Ets case)

Conviction exits when its SINGLE trigger wallet sells (exit_reason='trigger_wallet_exit').
But other roster/watch wallets often keep accumulating the same token while we hold and
after we sell. This measures, for each trigger_wallet_exit trade, the token's forward
price AFTER our exit (Birdeye 1H) and whether OTHER wallets were buying at exit.

If tokens run after we exit — ESPECIALLY when others are still buying at exit — conviction
should switch from single-wallet-sell to a COHORT-sell exit (like cluster's sell_cluster):
hold while the crowd accumulates, exit when the crowd leaves. Read-only. Runs in bot_copy.
"""
import json
import time
import urllib.request
import urllib.error
import statistics as st

from sqlalchemy import text
from framework.db import session_scope
from bots.copy.config import get_copy_settings

KEY = get_copy_settings().birdeye_api_key
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


def _be(path, tries=3):
    for _ in range(tries):
        try:
            r = urllib.request.Request(
                "https://public-api.birdeye.so" + path,
                headers={"X-API-KEY": KEY, "x-chain": "solana", "Accept": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(r, timeout=25) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5)
                continue
            return None
        except Exception:
            time.sleep(2)
    return None


def hist_1h(mint, t_from, t_to):
    h = _be(f"/defi/history_price?address={mint}&address_type=token&type=1H&time_from={t_from}&time_to={t_to}")
    items = ((h or {}).get("data") or {}).get("items") or []
    return sorted([(it["unixTime"], float(it["value"])) for it in items if it.get("value")], key=lambda x: x[0])


SQL = """
SELECT t.id, t.asset,
  EXTRACT(EPOCH FROM t.exit_at)::bigint AS exit_unix,
  t.exit_price, t.pnl_usd,
  (SELECT count(*) FROM wallet_swaps_log w
   WHERE w.token_mint = t.asset AND w.side = 'buy'
     AND (t.sim_metadata->>'trigger_wallet' IS NULL OR w.wallet_address <> t.sim_metadata->>'trigger_wallet')
     AND w.event_at BETWEEN t.exit_at - interval '1 hour' AND t.exit_at + interval '1 hour') AS cobuy_at_exit
FROM trades t
WHERE t.bot_id = 'copy' AND t.mode = 'paper' AND t.sim_metadata->>'strategy' = 'conviction'
  AND t.fill_status = 'closed' AND t.exit_reason = 'trigger_wallet_exit'
  AND t.exit_price > 0 AND t.exit_at IS NOT NULL
"""


def med(xs):
    return round(st.median(xs), 2) if xs else None


def pct_ge(xs, k):
    return round(100 * sum(1 for x in xs if x >= k) / len(xs)) if xs else None


def main():
    with session_scope() as s:
        rows = [dict(r) for r in s.execute(text(SQL)).mappings()]
    print(f"conviction trigger_wallet_exit trades: {len(rows)}")
    now = int(time.time())
    rec = []  # (fwd_from_exit, cobuy_at_exit, our_pnl)
    nodata = 0
    for r in rows:
        ex = int(r["exit_unix"])
        exp = float(r["exit_price"])
        pts = hist_1h(r["asset"], ex - 1800, min(now, ex + 48 * 3600))
        time.sleep(0.2)
        after = [p for t, p in pts if t >= ex]
        if not after or exp <= 0:
            nodata += 1
            continue
        rec.append((max(after) / exp, int(r["cobuy_at_exit"]), float(r["pnl_usd"])))
    print(f"no-data/skipped (illiquid/no-history): {nodata}\n")
    allf = [x[0] for x in rec]
    cob = [x[0] for x in rec if x[1] > 0]
    noc = [x[0] for x in rec if x[1] == 0]
    print(f"=== forward max price AFTER our trigger_wallet_exit (n={len(rec)}) ===")
    print(f"    median {med(allf)}x   |   {pct_ge(allf,2)}% ran >=2x after we sold   |   {pct_ge(allf,1.5)}% >=1.5x")
    print(f"    -> we left this much on the table by exiting on the single wallet's sell.\n")
    print(f"=== conditioned on OTHER wallets buying at exit (the hold-signal) ===")
    print(f"    WITH co-buying (n={len(cob)}):  median {med(cob)}x, {pct_ge(cob,2)}% >=2x, {pct_ge(cob,1.5)}% >=1.5x")
    print(f"    NO co-buying   (n={len(noc)}):  median {med(noc)}x, {pct_ge(noc,2)}% >=2x, {pct_ge(noc,1.5)}% >=1.5x")
    print("\nREAD: if forward-max is well above 1x (esp. WITH co-buying), the single-wallet exit")
    print("is premature -> switch conviction to a cohort-sell exit.")


if __name__ == "__main__":
    main()
