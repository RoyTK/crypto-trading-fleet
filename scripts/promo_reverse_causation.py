"""Reverse-causation check for the promobuy BOTH-FEEDS signal (Roy 2026-07-18).

Question: when a token is on BOTH Dexscreener paid feeds (profile + boost), does the
SECOND feed LEAD the price run (=> a tradeable ENTRY trigger) or LAG it (=> the run
already happened; the 2nd feed is momentum-chasing => usable only as a HOLD-LONGER/
scale confirmation, not an entry)?

Method: for both-feeds tokens with flip timestamps, take feed #1 and feed #2 times.
Pull Birdeye 1H price history and measure per token:
  run_before_2nd = price@2nd / price@1st    (how much it already moved BY feed #2)
  fwd_from_2nd   = maxprice_after2 / price@2nd  (upside still left if you ENTER on #2)
Aggregate by ordering. LEAD => fwd_from_2nd high & run_before modest. LAG => fwd_from_2nd
~1 & run_before large. Read-only. Runs in bot_copy (birdeye_api_key + DB).
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


def price_at(pts, ts, tol=7200):
    best, bd = None, tol + 1
    for t, p in pts:
        d = abs(t - ts)
        if d < bd:
            bd, best = d, p
    return best


SQL = """
SELECT token,
  EXTRACT(EPOCH FROM first_profiled_at)::bigint AS profiled_unix,
  EXTRACT(EPOCH FROM first_boosted_at)::bigint  AS boosted_unix,
  price_at_signal, fwd_mult_max,
  CASE WHEN first_boosted_at < first_profiled_at THEN 'boost_first'
       WHEN first_profiled_at < first_boosted_at THEN 'profile_first' ELSE 'same' END AS ord
FROM promo_shadow_signals
WHERE is_boosted AND is_profiled AND first_boosted_at IS NOT NULL AND first_profiled_at IS NOT NULL
  AND fwd_mult_max IS NOT NULL AND price_at_signal > 0
"""


def med(xs):
    return round(st.median(xs), 2) if xs else None


def main():
    with session_scope() as s:
        rows = [dict(r) for r in s.execute(text(SQL)).mappings()]
    print(f"both-feeds tokens with timestamps: {len(rows)}")
    now = int(time.time())
    res = {"profile_first": [], "same": [], "boost_first": []}
    nodata = 0
    for r in rows:
        t1 = min(r["profiled_unix"], r["boosted_unix"])   # first feed
        t2 = max(r["profiled_unix"], r["boosted_unix"])   # second feed (entry candidate)
        pts = hist_1h(r["token"], t1 - 3600, min(now, t2 + 4 * 86400))
        time.sleep(0.2)
        if not pts:
            nodata += 1
            continue
        p1 = price_at(pts, t1) or float(r["price_at_signal"])
        p2 = price_at(pts, t2)
        peak_after = max((p for t, p in pts if t >= t2), default=None)
        if not p1 or not p2 or p1 <= 0 or p2 <= 0 or not peak_after:
            nodata += 1
            continue
        res[r["ord"]].append((p2 / p1, peak_after / p2, float(r["fwd_mult_max"]), (t2 - t1) / 3600.0))
    print(f"no-data/skipped (illiquid/rugged/no-history): {nodata}\n")
    for k in ("profile_first", "same", "boost_first"):
        v = res[k]
        if not v:
            print(f"=== {k}: (none)\n")
            continue
        rb = [x[0] for x in v]
        f2 = [x[1] for x in v]
        fp = [x[2] for x in v]
        gap = [x[3] for x in v]
        pct2 = round(100 * sum(1 for x in f2 if x >= 2) / len(f2))
        big_runup = round(100 * sum(1 for x in rb if x >= 2) / len(rb))
        print(f"=== {k}   n={len(v)}   median gap 1st->2nd feed = {med(gap)}h")
        print(f"    run BEFORE 2nd feed  price@2nd / price@1st  : median {med(rb)}x  ({big_runup}% already >=2x by feed #2)")
        print(f"    fwd FROM 2nd feed    peakAfter / price@2nd  : median {med(f2)}x  ({pct2}% >= 2x)   <-- upside if you ENTER on feed #2")
        print(f"    (ref) fwd from 1st feed (stored)           : median {med(fp)}x\n")
    print("READ: LEAD/entry if fwd-from-2nd is high & run-before modest;  LAG/hold-only if fwd-from-2nd ~1 & run-before already large.")


if __name__ == "__main__":
    main()
