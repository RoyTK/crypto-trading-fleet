"""Classify every follow-roster wallet by STYLE, to (A) re-tier the fleet (demote
unfollowable snipers/MMs) and (B) isolate the SELECTOR cohort to study.

Discriminator = median token AGE AT ENTRY on the wallet's biggest positions
(sample top-N by bought_usd) + activity signature:
  - SNIPER          median age <= 15min           -> launch snipe (MEV; unfollowable)
  - MM_ESTABLISHED  median age >= 7d               -> buys old tokens, spread/volume
  - MM_HFT          >20k swaps OR >1500 tokens      -> high-freq market-maker
  - SELECTOR        15min < age <= 72h             -> hours-old runner-picker (CAPTURABLE)
  - OTHER           72h..7d or no age data
Only SELECTORs are worth following / studying; snipers+MMs are the adverse-selection source.

Resumable (disk creation cache). Read-only (writes /tmp/wallet_style.json).
Run inside bot_copy: docker compose exec -T bot_copy python scripts/wallet_style_classify.py
Env: SAMPLE (top-N tokens/wallet, default 10), MAX_TOK (cap unique creation fetches, default 9000).
"""
import os, time, json, urllib.request, urllib.error
from statistics import median
from sqlalchemy import text
from framework.db import session_scope
from bots.copy.config import get_copy_settings

KEY = get_copy_settings().birdeye_api_key
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
SAMPLE = int(os.getenv("SAMPLE", "10"))
MAX_TOK = int(os.getenv("MAX_TOK", "9000"))
CACHE = os.getenv("CREATION_CACHE", "/tmp/creation_cache.json")

ROSTER_SQL = "SELECT address, tier, swing, conviction FROM wallet_pool WHERE tier IN ('active','teamfollow') OR swing=true OR conviction=true"

# top-N tokens per roster wallet by bought_usd + first-buy time
SAMPLE_SQL = """
WITH r AS (SELECT address FROM wallet_pool WHERE tier IN ('active','teamfollow') OR swing=true OR conviction=true),
t AS (
  SELECT wallet_address, token_mint,
    SUM(notional_usd) FILTER (WHERE side='buy') AS bought,
    EXTRACT(EPOCH FROM MIN(event_at) FILTER (WHERE side='buy'))::bigint AS first_buy_u,
    row_number() OVER (PARTITION BY wallet_address
      ORDER BY SUM(notional_usd) FILTER (WHERE side='buy') DESC NULLS LAST) AS rn
  FROM wallet_swaps_log WHERE wallet_address IN (SELECT address FROM r)
  GROUP BY wallet_address, token_mint)
SELECT wallet_address, token_mint, first_buy_u FROM t WHERE rn <= :n AND first_buy_u IS NOT NULL
"""

# cheap per-wallet activity signature
ACT_SQL = """
WITH r AS (SELECT address FROM wallet_pool WHERE tier IN ('active','teamfollow') OR swing=true OR conviction=true)
SELECT wallet_address,
  COUNT(*) AS swaps,
  COUNT(DISTINCT token_mint) AS tokens,
  ROUND((COUNT(*) FILTER (WHERE side='buy')::float / NULLIF(COUNT(DISTINCT token_mint),0))::numeric,1) AS buys_per_tok
FROM wallet_swaps_log WHERE wallet_address IN (SELECT address FROM r)
GROUP BY wallet_address
"""


def _be(path, tries=3):
    for _ in range(tries):
        try:
            r = urllib.request.Request("https://public-api.birdeye.so" + path,
                headers={"X-API-KEY": KEY, "x-chain": "solana", "Accept": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(r, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(4); continue
            return None
        except Exception:
            time.sleep(1)
    return None


def creation_unix(mint):
    d = _be(f"/defi/token_creation_info?address={mint}")
    v = ((d or {}).get("data") or {}).get("blockUnixTime")
    return int(v) if v is not None else None


def classify(median_age_h, swaps, tokens):
    if swaps > 20000 or tokens > 1500:
        return "MM_HFT"
    if median_age_h is None:
        return "OTHER"
    if median_age_h <= 0.25:
        return "SNIPER"
    if median_age_h >= 168:
        return "MM_ESTABLISHED"
    if median_age_h <= 72:
        return "SELECTOR"
    return "OTHER"


def main():
    with session_scope() as s:
        roster = {r.address: dict(tier=r.tier, swing=r.swing, conviction=r.conviction)
                  for r in s.execute(text(ROSTER_SQL))}
        act = {r.wallet_address: dict(swaps=int(r.swaps), tokens=int(r.tokens),
                                      buys_per_tok=float(r.buys_per_tok or 0))
               for r in s.execute(text(ACT_SQL))}
        samples = {}
        for r in s.execute(text(SAMPLE_SQL), {"n": SAMPLE}):
            samples.setdefault(r.wallet_address, []).append((r.token_mint, int(r.first_buy_u)))

    try:
        cache = json.load(open(CACHE))
    except Exception:
        cache = {}
    # unique tokens to fetch
    uniq = []
    for toks in samples.values():
        for mint, _ in toks:
            if mint not in cache:
                uniq.append(mint)
    uniq = list(dict.fromkeys(uniq))[:MAX_TOK]
    print(f"roster {len(roster)} wallets | {len(uniq)} new tokens to fetch creation (cache {len(cache)})")
    for i, mint in enumerate(uniq, 1):
        cache[mint] = creation_unix(mint)
        time.sleep(0.1)
        if i % 250 == 0:
            json.dump(cache, open(CACHE, "w"))
            print(f"  ...{i}/{len(uniq)} fetched")
    json.dump(cache, open(CACHE, "w"))

    out = {}
    counts = {}
    for w, meta in roster.items():
        a = act.get(w, dict(swaps=0, tokens=0, buys_per_tok=0))
        ages = []
        for mint, fb in samples.get(w, []):
            cu = cache.get(mint)
            if cu:
                ages.append((fb - cu) / 3600.0)
        med_age = median(ages) if ages else None
        cls = classify(med_age, a["swaps"], a["tokens"])
        counts[cls] = counts.get(cls, 0) + 1
        out[w] = dict(cls=cls, median_age_h=(round(med_age, 2) if med_age is not None else None),
                      n_aged=len(ages), swaps=a["swaps"], tokens=a["tokens"],
                      buys_per_tok=a["buys_per_tok"], tier=meta["tier"],
                      swing=meta["swing"], conviction=meta["conviction"])
    json.dump(out, open("/tmp/wallet_style.json", "w"))

    print("\n=== STYLE COUNTS (of follow-roster wallets) ===")
    for k in ("SNIPER", "MM_HFT", "MM_ESTABLISHED", "SELECTOR", "OTHER"):
        print(f"  {k:16} {counts.get(k,0)}")
    # how many CURRENTLY-active wallets are unfollowable
    unfoll = [w for w, v in out.items() if v["cls"] in ("SNIPER", "MM_HFT", "MM_ESTABLISHED") and v["tier"] == "active"]
    sel_active = [w for w, v in out.items() if v["cls"] == "SELECTOR" and v["tier"] == "active"]
    print(f"\nactive-tier UNFOLLOWABLE (sniper/MM): {len(unfoll)}  |  active-tier SELECTORS: {len(sel_active)}")
    print("wallet_style.json written.")


if __name__ == "__main__":
    main()
