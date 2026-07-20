"""v2 held-accumulator roster builder — fixes the noise the v1 corpus scan exposed
(Roy 2026-07-20). Four changes vs build_accumulator_roster.py:

  1. DEDUPE BY MINT — scan each distinct token once (v1 double-counted a mint that had
     multiple prerun_scans run_date rows).
  2. REQUIRE >=2 DISTINCT TICKERS — a wallet must hold early bags in >=2 different SYMBOLS,
     not two same-ticker launches (v1's "held only HOOD / only Udin" artifact).
  3. EXCLUDE INFRA — drop any wallet whose single-token holding is > $1M (CEX / bridge /
     program, e.g. v1's $7.95M AAgeUpYY), not a trader.
  4. PEAK-VALUE FLOOR — value a wallet's CURRENT bag at the token's PEAK price, not current
     price, so early holders of runners that ran-then-FADED (HOOD 2991x, ANSEM 1472x — v1
     showed ~0 accums because bags fell below the current-$ floor) are no longer invisible.
     Combined with the early-buyer filter, this means "bought early + held a position that
     was once real", which is the accumulator signal.

Read-only. Usage (bot_copy):
  docker compose exec -T bot_copy python -m scripts.build_accumulator_roster_v2 [min_run_x] [min_peak_usd] [limit]
  defaults: min_run_x=5  min_peak_usd=2000  limit=0(all)
"""
import sys
import time
from collections import defaultdict

from sqlalchemy import text

from framework.db import session_scope
from scripts.find_held_accumulators import _be

INFRA_CAP_USD = 1_000_000.0   # single-token holding above this = CEX/bridge/program, skip
N_EARLY = 500


def _first_buyers(mint):
    out = {}
    for off in range(0, N_EARLY, 100):
        fb = _be(f"/token/v1/first-buyers?token_address={mint}&limit=100&offset={off}")
        buyers = (fb.get("data") or {}).get("buyers") or []
        for b in buyers:
            w = b.get("wallet_address")
            if w and w not in out:
                out[w] = b.get("tags") or []
        time.sleep(0.3)
        if len(buyers) < 100:
            break
    return out


def _holders(mint):
    out = {}
    for off in (0, 100):
        d = _be(f"/defi/v3/token/holder?address={mint}&offset={off}&limit=100")
        for h in (d.get("data") or {}).get("items") or []:
            w = h.get("owner") or h.get("address")
            if w:
                out[w] = float(h.get("ui_amount") or h.get("uiAmount") or 0)
        time.sleep(0.3)
    return out


def _peak_price(mint):
    now = int(time.time())
    h = _be(f"/defi/history_price?address={mint}&address_type=token&type=1H"
            f"&time_from={now - 95 * 86400}&time_to={now}")
    vals = [float(it["value"]) for it in ((h or {}).get("data") or {}).get("items") or [] if it.get("value")]
    return max(vals) if vals else 0.0


def main():
    min_run_x = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    min_peak = float(sys.argv[2]) if len(sys.argv) > 2 else 2000.0
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    with session_scope() as s:
        rows = s.execute(text(
            "SELECT DISTINCT ON (token) token, symbol, run_x FROM prerun_scans "
            "WHERE run_x >= :x ORDER BY token, run_x DESC"), {"x": min_run_x}).all()
    rows = sorted(rows, key=lambda r: -(r[2] or 0))
    if limit:
        rows = rows[:limit]
    print(f"distinct runner mints to scan: {len(rows)}  "
          f"(run_x>={min_run_x}, peak_floor=${min_peak:,.0f}, infra_cap=${INFRA_CAP_USD:,.0f})", flush=True)

    tally = defaultdict(dict)   # wallet -> {symbol: (peak_value, tags, clean)}
    in_pool = {}
    with session_scope() as s:
        pool = {a for (a,) in s.execute(text("SELECT address FROM wallet_pool")).all()}

    for i, (mint, symbol, run_x) in enumerate(rows, 1):
        sym = symbol or mint[:6]
        try:
            cur = float((_be("/defi/price?address=" + mint).get("data") or {}).get("value") or 0)
            time.sleep(0.2)
            peak = _peak_price(mint)
            time.sleep(0.2)
            early = _first_buyers(mint)
            holders = _holders(mint)
        except Exception as e:
            print(f"  [{i}/{len(rows)}] {sym[:12]:<12} ERR {str(e)[:45]}", flush=True)
            continue
        q = 0
        for w in early:
            bal = holders.get(w, 0)
            if bal <= 0:
                continue
            if bal * cur > INFRA_CAP_USD:        # infra exclude
                continue
            pv = bal * peak
            if pv < min_peak:                    # peak-value floor
                continue
            tags = early[w]
            clean = ("smart_trader" in tags and "bundler" not in tags)
            # keep the richest hit per (wallet,symbol)
            prev = tally[w].get(sym)
            if prev is None or pv > prev[0]:
                tally[w][sym] = (pv, tuple(tags), clean)
            in_pool[w] = w in pool
            q += 1
        print(f"  [{i}/{len(rows)}] {sym[:12]:<12} run {run_x:>7,.0f}x  qualifiers={q}", flush=True)

    # recurrence = >=2 DISTINCT tickers
    recur = [(w, d) for w, d in tally.items() if len(d) >= 2]
    recur.sort(key=lambda kv: (-len(kv[1]), -sum(v[0] for v in kv[1].values())))
    print(f"\n=== held-accumulators of >=2 DISTINCT-ticker runners: {len(recur)} ===")
    print(f"{'wallet':<14} {'#tick':>5} {'clean':>5} {'peak$sum':>12}  pool  tickers")
    roster = []
    for w, d in recur:
        nclean = sum(1 for v in d.values() if v[2])
        tot = sum(v[0] for v in d.values())
        print(f"{w[:14]:<14} {len(d):>5} {nclean:>5} {tot:>12,.0f}  "
              f"{'Y' if in_pool.get(w) else 'N'}     {','.join(sorted(d))[:44]}")
        if nclean >= 2 and not in_pool.get(w):
            roster.append(w)
    print(f"\n=== clean-smart accumulators of >=2 distinct tickers, NOT in pool = conviction candidates: {len(roster)} ===")
    for w in roster:
        print("  " + w)


if __name__ == "__main__":
    main()
