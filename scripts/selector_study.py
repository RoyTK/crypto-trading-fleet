"""Phase 2: what conditions do the hours-old SELECTOR wallets buy into — and which predict a run?

Output feeds the rebuilt conviction strategy (Roy 2026-09-29): trade the CONDITIONS directly
instead of following a wallet's transaction.

Unit = one row per token, at t0 = the FIRST buy of that token by any SELECTOR wallet
(wallet_style.cls = 'SELECTOR'): the earliest moment a condition-based entry could act.
Random, deterministic sample (md5 order) of tokens whose t0 is > 7 days old.

Features — only what was KNOWABLE at t0 (no look-ahead):
  token_age_h           t0 - token creation (Birdeye token_creation_info, cached)
  runup_1h/6h/24h       price at t0 / price 1h/6h/24h earlier (15m history)
  runup_from_low        price at t0 / lowest price in the prior 24h
  pre_wallets_24h       distinct tracked wallets that bought in the 24h before t0 (wallet_swaps_log)
  pre_snipers / pre_selectors / pre_mm   … of those, by wallet style
  mins_since_first_tracked   t0 - first buy by ANY tracked wallet
  sel_buy_usd           size of the first selector's buy
  promo_before, promo_lead_h  promo_shadow_signals first seen before t0, and how long before
Outcome (entry = price one 15m bar AFTER t0, i.e. our reaction delay priced in):
  peak_24h, peak_7d     max price / entry within 24h / 7d
  hit2x_first           reached 2x BEFORE falling to 0.5x (a tradeable win, not just a spike)
  min_7d                min price / entry within 7d
Resumable (appends to OUT, skips tokens already done). Read-only on the DB.
Run in bot_copy:  N=2500 python scripts/selector_study.py
"""
import json
import os
import time
import urllib.error
import urllib.request
from datetime import timedelta

from sqlalchemy import text

from bots.copy.config import get_copy_settings
from framework.db import session_scope

KEY = get_copy_settings().birdeye_api_key
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
N = int(os.getenv("N", "2500"))
OUT = os.getenv("OUT", "/tmp/selector_study.jsonl")
CACHE = os.getenv("CREATION_CACHE", "/tmp/creation_cache.json")

SAMPLE_SQL = """
WITH e AS (
  SELECT s.token_mint, min(s.event_at) FILTER (WHERE s.side = 'buy') AS t0
  FROM wallet_swaps_log s JOIN wallet_style ws ON ws.address = s.wallet_address AND ws.cls = 'SELECTOR'
  GROUP BY 1)
SELECT token_mint, t0 FROM e
WHERE t0 IS NOT NULL AND t0 < now() - interval '7 days'
ORDER BY md5(token_mint) LIMIT :n
"""

FEAT_SQL = """
WITH first_sel AS (
  SELECT s.notional_usd FROM wallet_swaps_log s JOIN wallet_style ws ON ws.address = s.wallet_address
  WHERE s.token_mint = :tok AND s.side = 'buy' AND ws.cls = 'SELECTOR' AND s.event_at = :t0 LIMIT 1),
pre AS (
  SELECT DISTINCT s.wallet_address, COALESCE(ws.cls, 'UNCLASSIFIED') AS cls
  FROM wallet_swaps_log s LEFT JOIN wallet_style ws ON ws.address = s.wallet_address
  WHERE s.token_mint = :tok AND s.side = 'buy' AND s.event_at >= :t0 - interval '24 hours' AND s.event_at < :t0)
SELECT
  (SELECT count(*) FROM pre) AS pre_wallets_24h,
  (SELECT count(*) FROM pre WHERE cls = 'SNIPER') AS pre_snipers,
  (SELECT count(*) FROM pre WHERE cls = 'SELECTOR') AS pre_selectors,
  (SELECT count(*) FROM pre WHERE cls IN ('MM_HFT', 'MM_ESTABLISHED')) AS pre_mm,
  (SELECT EXTRACT(EPOCH FROM :t0 - min(event_at)) / 60 FROM wallet_swaps_log
     WHERE token_mint = :tok AND side = 'buy' AND event_at <= :t0) AS mins_since_first_tracked,
  (SELECT notional_usd FROM first_sel) AS sel_buy_usd,
  (SELECT signal_at FROM promo_shadow_signals WHERE token = :tok) AS promo_at
"""


def _be(path, tries=3):
    for _ in range(tries):
        try:
            r = urllib.request.Request("https://public-api.birdeye.so" + path, headers={
                "X-API-KEY": KEY, "x-chain": "solana", "Accept": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(r, timeout=25) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(4)
                continue
            return None
        except Exception:
            time.sleep(1)
    return None


def creation_unix(mint, cache):
    if mint in cache:
        return cache[mint]
    d = _be(f"/defi/token_creation_info?address={mint}")
    v = ((d or {}).get("data") or {}).get("blockUnixTime")
    cache[mint] = int(v) if v is not None else None
    return cache[mint]


def hist(mint, t0u, t1u, typ="15m"):
    h = _be(f"/defi/history_price?address={mint}&address_type=token&type={typ}&time_from={t0u}&time_to={t1u}")
    items = ((h or {}).get("data") or {}).get("items") or []
    return sorted((int(i["unixTime"]), float(i["value"])) for i in items if i.get("value"))


def price_at(series, t):
    prev = None
    for u, p in series:
        if u <= t:
            prev = p
        else:
            break
    return prev


def main():
    done = set()
    if os.path.exists(OUT):
        with open(OUT) as fh:
            done = {json.loads(line)["token"] for line in fh if line.strip()}
    try:
        cache = json.load(open(CACHE))
    except Exception:
        cache = {}
    with session_scope() as s:
        sample = [(r.token_mint, r.t0) for r in s.execute(text(SAMPLE_SQL), {"n": N})]
    todo = [(m, t) for m, t in sample if m not in done]
    print(f"sample {len(sample)} tokens, {len(done)} done, {len(todo)} to go", flush=True)
    out = open(OUT, "a")
    for i, (mint, t0) in enumerate(todo, 1):
        t0u = int(t0.timestamp())
        with session_scope() as s:
            f = dict(s.execute(text(FEAT_SQL), {"tok": mint, "t0": t0}).mappings().one())
        cu = creation_unix(mint, cache)
        series = hist(mint, t0u - 24 * 3600, t0u + 7 * 86400 + 3600)
        time.sleep(0.12)
        row = {"token": mint, "t0": t0.isoformat(),
               "token_age_h": round((t0u - cu) / 3600, 3) if cu else None,
               "pre_wallets_24h": f["pre_wallets_24h"], "pre_snipers": f["pre_snipers"],
               "pre_selectors": f["pre_selectors"], "pre_mm": f["pre_mm"],
               "mins_since_first_tracked": float(f["mins_since_first_tracked"] or 0),
               "sel_buy_usd": float(f["sel_buy_usd"]) if f["sel_buy_usd"] is not None else None,
               "promo_before": bool(f["promo_at"] and f["promo_at"] < t0),
               "promo_lead_h": (round((t0 - f["promo_at"]).total_seconds() / 3600, 2)
                                if f["promo_at"] and f["promo_at"] < t0 else None),
               "n_points": len(series)}
        p0 = price_at(series, t0u)
        if p0 and len(series) >= 8:
            for lbl, secs in (("runup_1h", 3600), ("runup_6h", 6 * 3600), ("runup_24h", 24 * 3600)):
                pb = price_at(series, t0u - secs)
                row[lbl] = round(p0 / pb, 4) if pb else None
            lows = [p for u, p in series if u <= t0u]
            row["runup_from_low"] = round(p0 / min(lows), 4) if lows else None
            fwd = [(u, p) for u, p in series if u > t0u]
            if fwd:
                pe = fwd[0][1]                         # next 15m bar = entry after our reaction delay
                after = [(u, p / pe) for u, p in fwd[1:] if u <= t0u + 7 * 86400] or [(fwd[0][0], 1.0)]
                row["peak_24h"] = round(max([m for u, m in after if u <= t0u + 86400] or [1.0]), 4)
                row["peak_7d"] = round(max(m for _, m in after), 4)
                row["min_7d"] = round(min(m for _, m in after), 4)
                t2 = next((u for u, m in after if m >= 2), None)
                th = next((u for u, m in after if m <= 0.5), None)
                row["hit2x_first"] = bool(t2 and (th is None or t2 < th))
        out.write(json.dumps(row) + "\n")
        out.flush()
        if i % 100 == 0:
            json.dump(cache, open(CACHE, "w"))
            print(f"  {i}/{len(todo)}", flush=True)
    json.dump(cache, open(CACHE, "w"))
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
