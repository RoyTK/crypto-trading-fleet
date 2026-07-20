"""Industrialize held-accumulator sourcing: loop find_held_accumulators over the runner
corpus (prerun_scans) and tally wallets that recur as held-accumulators across MANY
runners = the conviction roster candidates. (Roy 2026-07-19 "circle back" — the whole
point: a wallet holding ONE runner is a coin-flip; one holding early bags in 3+ is a
signal.) Read-only.

CAVEAT (honest): held_accumulators uses CURRENT holder value, so a wallet that accumulated
a runner which then ran-and-faded now shows a small bag and drops below the value floor.
So recurrence here is BIASED toward still-live runners and UNDERCOUNTS accumulators of
faded ones — treat as a directional floor, iterate the metric (e.g. peak-value, or
sell-behaviour from first-buyers) when we circle back.

Usage (bot_copy):
  docker compose exec -T bot_copy python -m scripts.build_accumulator_roster [min_run_x] [min_value_usd] [limit]
  defaults: min_run_x=5  min_value_usd=500  limit=0(all)
"""
import sys
from collections import defaultdict

from sqlalchemy import text

from framework.db import session_scope
from scripts.find_held_accumulators import held_accumulators


def main():
    min_run_x = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    min_val = float(sys.argv[2]) if len(sys.argv) > 2 else 500.0
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    with session_scope() as s:
        rows = s.execute(text(
            "SELECT token, symbol, run_x FROM prerun_scans "
            "WHERE run_x >= :x ORDER BY run_date DESC"), {"x": min_run_x}).all()
    if limit:
        rows = rows[:limit]
    print(f"runners to scan: {len(rows)}  (run_x>={min_run_x}, min_hold=${min_val:,.0f})", flush=True)

    tally = defaultdict(list)   # wallet -> [(symbol, value, tags_tuple, clean_bool)]
    in_pool = {}
    for i, (token, symbol, run_x) in enumerate(rows, 1):
        try:
            accs, _price = held_accumulators(token, min_value_usd=min_val)
        except Exception as e:
            print(f"  [{i}/{len(rows)}] {(symbol or token)[:12]:<12} ERR {str(e)[:50]}", flush=True)
            continue
        for a in accs:
            tally[a["wallet"]].append((symbol or token[:6], a["value_usd"], tuple(a["tags"]), a["clean_smart"]))
            in_pool[a["wallet"]] = a["in_pool"]
        clean = sum(1 for a in accs if a["clean_smart"])
        print(f"  [{i}/{len(rows)}] {(symbol or token)[:12]:<12} run {run_x:>7,.0f}x  "
              f"held-accums={len(accs):>2} clean={clean}", flush=True)

    recur = [(w, hits) for w, hits in tally.items() if len(hits) >= 2]
    recur.sort(key=lambda kv: (-len(kv[1]), -sum(h[1] for h in kv[1])))
    print(f"\n=== held-accumulators of >=2 runners: {len(recur)} ===")
    print(f"{'wallet':<14} {'#runs':>5} {'clean':>5} {'tot$held':>11}  pool  runners")
    roster = []
    for w, hits in recur:
        nclean = sum(1 for h in hits if h[3])
        tot = sum(h[1] for h in hits)
        syms = ",".join(sorted({h[0] for h in hits}))[:46]
        print(f"{w[:14]:<14} {len(hits):>5} {nclean:>5} {tot:>11,.0f}  "
              f"{'Y' if in_pool.get(w) else 'N'}     {syms}")
        if nclean >= 2 and not in_pool.get(w):
            roster.append(w)

    print(f"\n=== clean-smart accumulators of >=2 runners, NOT in pool = conviction candidates: {len(roster)} ===")
    for w in roster:
        print("  " + w)


if __name__ == "__main__":
    main()
