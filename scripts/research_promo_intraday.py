"""Intraday promo-vs-run-start classification (info-source study, Phase 1).

The day-granular pass showed 33 runners whose FIRST Dexscreener paid-promo order
landed within [-3d, +1d] of run_date — the bucket where day precision can't tell
"promo before the run ignited" (actionable lead) from "promo chasing a pump already
underway". This script resolves it with Birdeye 1H candles:

  run_start_A: same rule as scrape_runners at hourly resolution — latest pre-peak
               hour with price <= PEAK_FRACTION(0.12) x window peak.
  ignition_B : first hour whose price >= 2x the median of the previous 24 hourly
               closes (needs >=12 prior points) — "the pump began here".

For each token: promo paymentTimestamp vs both definitions -> lead minutes
(positive = promo BEFORE run start = potential H1 lead). Also promo vs the
earliest genuine accumulator first_buy where we have it (precise H1/H2 check).

Run IN-CONTAINER on Hetzner (framework has ./scripts + ./bots mounts + Birdeye key):
  docker compose exec -T framework python -m scripts.research_promo_intraday
"""
from __future__ import annotations

import json
import statistics as st
import time
import urllib.request
from datetime import datetime, timezone, timedelta

from bots.copy.config import get_copy_settings

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
_KEY = get_copy_settings().birdeye_api_key
PEAK_FRACTION = 0.12
RATE_SLEEP = 1.1

# 33 runners whose first paid promo fell in [-3d,+1d] of run_date (day-granular pass
# 2026-07-11; source scratchpad/source_hits_full.json). t=token, s=symbol,
# rd=run_date, p=first promo payment ms, ab=earliest accumulator buy (or null).
SUBSET = json.loads(r'''
[{"t":"24Kko3VWwMKtkB9CVct2ZAZgJzb2R2iNrYxCpTYFpump","s":"APU","rd":"2026-06-05","p":1780667130986,"ab":null},{"t":"BcHEaaTCvycPwwsJ9yQTXdHP9X2gCLkznDbZ8VySpump","s":"Jotchua","rd":"2026-06-07","p":1780807770983,"ab":null},{"t":"5pYB12kEhfhSFXJjZ7JtyqDpt6uUqhsF6iu6Ee9spump","s":"CATWIF","rd":"2026-06-22","p":1782112590129,"ab":null},{"t":"FMqh9mqR6drPZqqW6wPqLHxX4rqNDWGhYLaMfoaJpump","s":"world","rd":"2026-06-22","p":1782237702263,"ab":null},{"t":"BDdzUjksj1J4bSnMveQ5tV9Up8A9c6YS1tHrxNw3pump","s":"FOMO","rd":"2026-06-28","p":1782671821456,"ab":null},{"t":"CuGJf6cfDfMh4UxVgNJ5KFQ6v8Wv3qrqop6cFKsGpump","s":"TATE","rd":"2026-06-28","p":1782676447775,"ab":null},{"t":"HULaBKR1eK3SA18FpfEav8Z2t7TJ1qEC775YHTLNpump","s":"DAVID","rd":"2026-06-28","p":1782684620027,"ab":null},{"t":"ERPtViqckt1zqpS2abUx67CgLcP54CkhBPYz3yRUpump","s":"yep","rd":"2026-06-29","p":1782735279996,"ab":null},{"t":"2aQKC1xiKpEyAWvzSDrULnYLVuybWyx7qrsnpP8hpump","s":"Udin","rd":"2026-06-29","p":1782800690757,"ab":null},{"t":"DdPrHYqM8Ueovnk9kAnAgoGhswkuaTqmxcoZzU3Zpump","s":"manlet","rd":"2026-07-01","p":1782774171115,"ab":"2026-06-29 22:31:22+00"},{"t":"48oT2QgpyPFUiJYXbvZTS1tVDp98R9uYAJX5ojNrpump","s":"SEMAN","rd":"2026-07-02","p":1783065136874,"ab":null},{"t":"CFPkPq1eYPR8GLzEo59wUbbMioX4bshaTQiSGzTSpump","s":"MENSA","rd":"2026-07-02","p":1782959300419,"ab":null},{"t":"2jz9E5JrEbxLg1RhU68aaSikDvpQurCEZz9BBF9rpump","s":"PATTYICE","rd":"2026-07-02","p":1782835819725,"ab":"2026-06-30 02:54:44+00"},{"t":"4MrsXQzaosYNyFd4wKDvgnC5xRtRqgXRrijFTGj9pump","s":"NORMIE","rd":"2026-07-03","p":1782996934495,"ab":"2026-07-02 12:49:57+00"},{"t":"96X4zg5T4NFWzTVFXHsadvYbxbzFX2Rqt3GXUv92pump","s":"Balloon","rd":"2026-07-03","p":1783082391619,"ab":null},{"t":"4Vn4cyu67VJouC3BqEu7AAQix8k7ryLrA6mMmkjjpump","s":"Z","rd":"2026-07-03","p":1783167252141,"ab":null},{"t":"42cXQvAAr7hcPBPWAS4ocVtDyeJ4Fa6gRR2uG4gppump","s":"TBB","rd":"2026-07-03","p":1782832099290,"ab":"2026-06-30 03:03:23+00"},{"t":"ESAYabnPzfaZGcwNtTL5RL1RBaoW7vYUNx5n6QfTpump","s":"Pigeon","rd":"2026-07-03","p":1782848473739,"ab":"2026-06-30 19:37:19+00"},{"t":"GbpARMvdpDpavgLJXzTSgWAcSta2FeEYfjAZZ91upump","s":"MURAD","rd":"2026-07-04","p":1783272816965,"ab":null},{"t":"CvqxZzFZqDKSpD1bNSkwQ6j9stSCBVgQ9GT4tLdKpump","s":"TOLY","rd":"2026-07-04","p":1783201439281,"ab":null},{"t":"4PRz3EwhbjrrX6YksMDuUzrXT51pr7CQtXNCravhpump","s":"ACM","rd":"2026-07-04","p":1783230637145,"ab":null},{"t":"6baGyq4HLbUn93MQUGFqBktpXP8BRjpoxSsAap4ppump","s":"LEVI","rd":"2026-07-05","p":1783309186452,"ab":null},{"t":"6UGNg119LV6YjSCQ5K6Z6jsLLLetpMJ1vPdhiU8qpump","s":"CURB","rd":"2026-07-05","p":1783261474959,"ab":null},{"t":"E2ueKQ3EDTTmCkUA17j3KeTb2u6VT91xiyECdKRzpump","s":"tolywifhat","rd":"2026-07-06","p":1783320989756,"ab":null},{"t":"4eKYoR1hBHnRaYyg2d57H36uUatRNyZr1NRP9ScNpump","s":"DONALT","rd":"2026-07-06","p":1783312206854,"ab":null},{"t":"CizcDa22HJjXybKLriibqP167GcJjqiDHDPtuiMmpump","s":"ok","rd":"2026-07-06","p":1783037824685,"ab":"2026-07-03 03:56:24+00"},{"t":"6xCtR2Eq1VumsoRdNutcfSQfLMk7xUa2BrMx18tqpump","s":"DEXBULL","rd":"2026-07-07","p":1783513897918,"ab":null},{"t":"22deNCri9bBae1GWZxdaDxipMWdwAUQ2ddvxdSmgpump","s":"CASHCAT","rd":"2026-07-07","p":1783483672911,"ab":"2026-07-01 20:33:37+00"},{"t":"9y6hXFBFN1fJGfAAYgkXVVfQATG1mEJsmTrCXcT6pump","s":"Silk","rd":"2026-07-08","p":1783512120407,"ab":null},{"t":"4ko5tSr5o3H4v1sFtjTSd9MPUW7yx5AFCpkNPoL6pump","s":"febu","rd":"2026-07-08","p":1783548435566,"ab":null},{"t":"J5ZognFJEepsWsCQPmqVchvEvsUMomEkxd8LbMrH2J7","s":"Bullscan","rd":"2026-07-08","p":1783535255199,"ab":null},{"t":"BtUiqGdsW3H47yvtyKSLhQfzVpwov3wyYY6qssU9pump","s":"reptilecoin","rd":"2026-07-09","p":1783639996882,"ab":null},{"t":"CgnQ8a1u99Gk7qoySMd2wrGPMPm9bdcSJNhqMEkYpump","s":"Loom","rd":"2026-07-09","p":1783474144346,"ab":"2026-07-08 10:01:59+00"}]
''')


def be(path: str, tries: int = 3):
    for i in range(tries):
        try:
            r = urllib.request.Request(
                "https://public-api.birdeye.so" + path,
                headers={"X-API-KEY": _KEY, "x-chain": "solana",
                         "Accept": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(r, timeout=25) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5 * (i + 1))
                continue
            print(f"    [http {e.code}]")
            return None
        except Exception as e:
            print(f"    [err {e}]")
            time.sleep(2)
    return None


def hourly(mint: str, run_date: str):
    d0 = datetime.fromisoformat(run_date).replace(tzinfo=timezone.utc)
    t_from = int((d0 - timedelta(days=4)).timestamp())
    t_to = int((d0 + timedelta(days=3)).timestamp())
    h = be(f"/defi/history_price?address={mint}&address_type=token"
           f"&type=1H&time_from={t_from}&time_to={t_to}")
    items = ((h or {}).get("data") or {}).get("items") or []
    pts = sorted((it["unixTime"], float(it["value"])) for it in items if it.get("value"))
    return pts


def run_start_A(pts):
    """Latest pre-peak hour with px <= PEAK_FRACTION * peak (scrape_runners rule @1H)."""
    if len(pts) < 6:
        return None
    peak_ts, peak_px = max(pts, key=lambda x: x[1])
    if peak_px <= 0:
        return None
    pre = [p for p in pts if p[0] <= peak_ts and p[1] <= PEAK_FRACTION * peak_px]
    return pre[-1][0] if pre else None


def ignition_B(pts):
    """First hour whose px >= 2x median of the previous 24 closes (>=12 prior pts)."""
    for i in range(12, len(pts)):
        prior = [p[1] for p in pts[max(0, i - 24):i]]
        med = st.median(prior)
        if med > 0 and pts[i][1] >= 2.0 * med:
            return pts[i][0]
    return None


def fmt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M") if ts else "  n/a  "


def main():
    rows = []
    for r in SUBSET:
        pts = hourly(r["t"], r["rd"])
        time.sleep(RATE_SLEEP)
        rsA = run_start_A(pts)
        rsB = ignition_B(pts)
        promo_s = r["p"] / 1000.0
        leadA = (rsA - promo_s) / 3600.0 if rsA else None   # + = promo BEFORE run start
        leadB = (rsB - promo_s) / 3600.0 if rsB else None
        rows.append({**r, "n_pts": len(pts), "rsA": rsA, "rsB": rsB,
                     "leadA_h": leadA, "leadB_h": leadB})
        print(f"  {r['s']:<12} pts={len(pts):3d} promo={fmt(int(promo_s))} "
              f"rsA={fmt(rsA)} rsB={fmt(rsB)} "
              f"leadA={leadA:+7.1f}h" if leadA is not None else
              f"  {r['s']:<12} pts={len(pts):3d} promo={fmt(int(promo_s))} rsA=n/a rsB={fmt(rsB)}")

    print("\n=== PROMO vs INTRADAY RUN START (positive lead = promo BEFORE ignition) ===")
    for name, key in (("A latest<=12%peak", "leadA_h"), ("B 2x-median ignition", "leadB_h")):
        vals = [x[key] for x in rows if x[key] is not None]
        if not vals:
            print(f"  [{name}] no data")
            continue
        before = sum(1 for v in vals if v > 0)
        print(f"  [{name}] n={len(vals)}  promo BEFORE run-start: {before}/{len(vals)} "
              f"({100*before/len(vals):.0f}%)  lead-h: median {st.median(vals):+.1f}, "
              f"p25 {sorted(vals)[len(vals)//4]:+.1f}, p75 {sorted(vals)[3*len(vals)//4]:+.1f}")

    print("\n=== PROMO vs EARLIEST ACCUMULATOR BUY (precise, this subset) ===")
    both = [(x, datetime.fromisoformat(x["ab"].replace("+00", "+00:00")).timestamp())
            for x in rows if x.get("ab")]
    for x, ab_s in both:
        d_h = (ab_s - x["p"] / 1000.0) / 3600.0  # + = promo BEFORE accumulator
        print(f"  {x['s']:<12} promo {'BEFORE' if d_h > 0 else 'AFTER '} earliest accum "
              f"by {abs(d_h):6.1f}h")
    if both:
        b = sum(1 for x, ab_s in both if ab_s > x["p"] / 1000.0)
        print(f"  -> promo before accumulators: {b}/{len(both)}")

    with open("/tmp/promo_intraday_results.json", "w") as f:
        json.dump(rows, f, indent=1, default=str)
    print("\nresults -> /tmp/promo_intraday_results.json")


if __name__ == "__main__":
    main()
