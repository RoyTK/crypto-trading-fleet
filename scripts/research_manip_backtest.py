"""Backtest the manipulation detectors against OUR OWN closed trades.

Calibration (not a go/no-go gate — the papers' N dwarfs ours). Two questions:
  Q1  Do RUNNERS (max peak >= 100%) show more manipulation than duds?
      (Paper 1 says 83% of >100% returners are manipulated → if so, manipulation
       is a RUNNER marker, NOT an avoid-signal.)
  Q2  Do OUR LOSERS show more manipulation than our winners? (entry-filter value)

Per distinct token in our closed trades: page Birdeye token swaps (buys+sells,
all owners) over [first_entry - PRE_H, first_entry + POST_H], run the detectors
(scripts.manipulation_detectors), join to outcome. Reuses scrape_runners' proven
seek_by_time paging + _leg_usd.

Run IN-CONTAINER (bot_copy: birdeye key + DB + repo):
  docker compose exec -T bot_copy python -m scripts.research_manip_backtest [--limit N]
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import time
import urllib.request
import urllib.error
from datetime import timezone

from sqlalchemy import text
from framework.db import session_scope
from bots.copy.config import get_copy_settings
from scripts.manipulation_detectors import analyze, lpi_signature

_KEY = get_copy_settings().birdeye_api_key
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126 Safari/537.36"
QUOTES = {"So11111111111111111111111111111111111111112",
          "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
          "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
PRE_H, POST_H = 6, 24
MAX_PAGES = 6
DUST_USD = 5.0
RATE = 1.0


def _leg_usd_local(it):
    q = it.get("quote") or {}
    if q.get("address") in QUOTES and q.get("uiAmount") and q.get("price"):
        return abs(float(q["uiAmount"])) * float(q["price"])
    b = it.get("base") or {}
    tp = it.get("tokenPrice")
    if b.get("uiAmount") and tp:
        return abs(float(b["uiAmount"])) * float(tp)
    return 0.0


def be(path, tries=3):
    for i in range(tries):
        try:
            r = urllib.request.Request("https://public-api.birdeye.so" + path,
                headers={"X-API-KEY": _KEY, "x-chain": "solana",
                         "Accept": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(r, timeout=25) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5 * (i + 1)); continue
            return None
        except Exception:
            time.sleep(1.5)
    return None


def window_trades(mint, entry_ts):
    lo, hi = entry_ts - PRE_H * 3600, entry_ts + POST_H * 3600
    before, oldest, out = hi, hi, []
    prices = []
    for _ in range(MAX_PAGES):
        t = be(f"/defi/txs/token/seek_by_time?address={mint}&before_time={before}"
               f"&tx_type=swap&limit=50")
        time.sleep(RATE)
        items = ((t or {}).get("data") or {}).get("items") or []
        if not items:
            break
        for it in items:
            ts = int(it.get("blockUnixTime") or 0)
            oldest = min(oldest, ts) if ts else oldest
            if ts < lo or ts > hi:
                continue
            base = it.get("base") or {}
            chg = float(base.get("uiChangeAmount") or 0)
            owner = it.get("owner")
            if not owner or chg == 0:
                continue
            usd = _leg_usd_local(it)
            if it.get("tokenPrice"):
                prices.append((ts, float(it["tokenPrice"])))
            out.append({"owner": owner, "side": "buy" if chg > 0 else "sell",
                        "base_amount": abs(chg), "usd": usd, "ts": ts})
        if oldest <= lo:
            break
        before = oldest - 1
    return out, prices


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="/tmp/manip_backtest.json")
    args = ap.parse_args()

    with session_scope() as s:
        rows = s.execute(text("""
            SELECT asset,
                   MAX(EXTRACT(EPOCH FROM entry_at))::bigint AS entry_ts,
                   MAX(pnl_pct) AS best_pnl_pct,
                   MAX((sim_metadata->>'peak_pct_since_entry')::numeric) AS best_peak,
                   BOOL_OR(pnl_usd > 0) AS any_win,
                   BOOL_OR(exit_reason IN ('rug_no_liquidity','hard_stop')) AS any_stop,
                   MIN(regexp_replace(sim_metadata->>'strategy','_pre_reset|_interim','')) AS fam
            FROM trades
            WHERE bot_id='copy' AND mode='paper' AND fill_status='closed'
              AND exit_at IS NOT NULL AND entry_at IS NOT NULL
            GROUP BY asset ORDER BY MAX(entry_at) DESC
        """)).fetchall()
    tokens = [dict(asset=r[0], entry_ts=int(r[1]), best_pnl_pct=float(r[2] or 0),
                   best_peak=float(r[3] or 0), any_win=r[4], any_stop=r[5], fam=r[6])
              for r in rows if r[1]]
    if args.limit:
        tokens = tokens[:args.limit]
    print(f"tokens: {len(tokens)}")

    results = []
    t0 = time.time()
    for i, tk in enumerate(tokens):
        trades, prices = window_trades(tk["asset"], tk["entry_ts"])
        rep = analyze(trades)
        lpi = (False, "no_data")
        if prices and len(trades) >= 3:
            prices.sort()
            p0, ppk = prices[0][1], max(p[1] for p in prices)
            winvol = sum(t["usd"] for t in trades)
            buyusd = sum(t["usd"] for t in trades if t["side"] == "buy")
            lpi = lpi_signature(p0, ppk, winvol, 0.0, buyusd, winvol, rep.n_owners)
        results.append({**tk, "n_tx": len(trades), "n_owners": rep.n_owners,
                        "wash_max": round(rep.wash_score_max, 1),
                        "wash_flag": rep.wash_flagged,
                        "zero_risk": round(rep.zero_risk_vol_frac, 3),
                        "circular": round(rep.circular_vol_frac, 3),
                        "lpi": lpi[0]})
        if (i + 1) % 25 == 0:
            print(f"  {i+1}/{len(tokens)} ({(time.time()-t0)/60:.0f} min)")

    json.dump(results, open(args.out, "w"), indent=1, default=str)
    covered = [r for r in results if r["n_tx"] >= 5]
    print(f"\ncovered (>=5 tx in window): {len(covered)}/{len(results)}")

    def bucketize(rs, key, label):
        print(f"\n=== {label} ===")
        groups = {}
        for r in rs:
            groups.setdefault(key(r), []).append(r)
        for g in sorted(groups):
            rs2 = groups[g]
            print(f"  {g:<18} n={len(rs2):>3}  "
                  f"wash_flag {100*sum(x['wash_flag'] for x in rs2)/len(rs2):>3.0f}%  "
                  f"zero_risk>=.5 {100*sum(x['zero_risk']>=.5 for x in rs2)/len(rs2):>3.0f}%  "
                  f"circular>=.9 {100*sum(x['circular']>=.9 for x in rs2)/len(rs2):>3.0f}%  "
                  f"lpi {100*sum(x['lpi'] for x in rs2)/len(rs2):>3.0f}%  "
                  f"med_zr {st.median([x['zero_risk'] for x in rs2]):.2f}")

    bucketize(covered, lambda r: "runner(peak>=100%)" if r["best_peak"] >= 100 else "dud(peak<100%)",
              "Q1  manipulation by RUN outcome")
    bucketize(covered, lambda r: "our_win" if r["any_win"] else "our_loss",
              "Q2  manipulation by OUR trade outcome")
    bucketize(covered, lambda r: r["fam"], "by strategy family")
    print(f"\nresults -> {args.out}")


if __name__ == "__main__":
    main()
