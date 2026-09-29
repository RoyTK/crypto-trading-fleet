"""Analyse scripts/selector_study.py output: which conditions at the first SELECTOR buy predict a run?

Usage (local):  python scripts/selector_study_analyze.py reports/selector_study_2026-09-29/selector_study.jsonl

Per feature bucket: n, hit2x_first rate with a Wilson 95% CI, lift vs base, and a bracketed
per-trade EV for a mechanical 2x-take-profit / 0.5x-stop exit, priced with the real 1.25%
fee per side:
  hit2x_first            -> +100%
  fell to 0.5x first     -> -50%
  neither within 7d      -> unknown exit; bracketed by min_7d (low) and 1.0 (flat) - reported
                            as EV_lo / EV_hi. The truth sits between; label it as a bracket.
Then a time split (first half of t0 vs second half) for any bucket that looks good, so a
rule is only proposed if it holds in both halves.
"""
import json
import math
import sys

FEE = 0.0125 * 2


def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def trade_ret(r, neither_mode):
    if r["hit2x_first"]:
        return 1.0 - FEE
    if r["min_7d"] <= 0.5:
        return -0.5 - FEE
    return (r["min_7d"] - 1 if neither_mode == "lo" else 0.0) - FEE


def stats(rows):
    n = len(rows)
    k = sum(r["hit2x_first"] for r in rows)
    lo, hi = wilson(k, n)
    ev_lo = sum(trade_ret(r, "lo") for r in rows) / n if n else 0
    ev_hi = sum(trade_ret(r, "hi") for r in rows) / n if n else 0
    return n, k, lo, hi, ev_lo, ev_hi


FEATURES = [
    ("token_age_h", lambda r: r["token_age_h"], [(0, .25), (.25, 1), (1, 6), (6, 24), (24, 72), (72, 1e12)]),
    ("runup_1h", lambda r: r.get("runup_1h"), [(0, .8), (.8, 1.2), (1.2, 2), (2, 1e12)]),
    ("runup_6h", lambda r: r.get("runup_6h"), [(0, .8), (.8, 1.2), (1.2, 2), (2, 5), (5, 1e12)]),
    ("runup_24h", lambda r: r.get("runup_24h"), [(0, .8), (.8, 1.2), (1.2, 2), (2, 5), (5, 1e12)]),
    ("runup_from_low", lambda r: r.get("runup_from_low"), [(1, 1.5), (1.5, 3), (3, 10), (10, 1e12)]),
    ("pre_wallets_24h", lambda r: r["pre_wallets_24h"], [(0, 1), (1, 3), (3, 10), (10, 1e12)]),
    ("pre_snipers", lambda r: r["pre_snipers"], [(0, 1), (1, 3), (3, 1e12)]),
    ("pre_selectors", lambda r: r["pre_selectors"], [(0, 1), (1, 2), (2, 1e12)]),
    ("pre_mm", lambda r: r["pre_mm"], [(0, 1), (1, 3), (3, 1e12)]),
    ("mins_since_first_tracked", lambda r: r["mins_since_first_tracked"], [(0, .01), (.01, 10), (10, 60), (60, 600), (600, 1e12)]),
    ("sel_buy_usd", lambda r: r["sel_buy_usd"], [(0, 200), (200, 1000), (1000, 5000), (5000, 1e12)]),
    ("promo_before", lambda r: int(r["promo_before"]), [(0, 1), (1, 2)]),
]


def table(rows, base, title):
    print(f"\n=== {title}  (n={len(rows)}) ===")
    print(f"{'feature':26} {'bucket':>16} {'n':>5} {'hit2x':>6} {'95% CI':>13} {'lift':>5} {'EV_lo':>7} {'EV_hi':>7}")
    for name, f, edges in FEATURES:
        for a, b in edges:
            s = [r for r in rows if f(r) is not None and a <= f(r) < b]
            if not s:
                continue
            n, k, lo, hi, el, eh = stats(s)
            bk = f"[{a:g},{'inf' if b >= 1e12 else f'{b:g}'})"
            print(f"{name:26} {bk:>16} {n:5} {k / n:6.2f} ({lo:.2f},{hi:.2f}) {(k / n) / base if base else 0:5.2f} {el:+7.2%} {eh:+7.2%}")


def main():
    path = sys.argv[1]
    rows = [json.loads(l) for l in open(path) if l.strip()]
    rows = [r for r in rows if "hit2x_first" in r]
    rows.sort(key=lambda r: r["t0"])
    n, k, lo, hi, el, eh = stats(rows)
    base = k / n
    print(f"ALL n={n} hit2x={base:.3f} CI=({lo:.3f},{hi:.3f}) EV=[{el:+.2%},{eh:+.2%}]  "
          f"t0 {rows[0]['t0'][:10]} .. {rows[-1]['t0'][:10]}")
    table(rows, base, "all")
    half = n // 2
    table(rows[:half], base, f"FIRST half (t0 <= {rows[half - 1]['t0'][:10]})")
    table(rows[half:], base, f"SECOND half (t0 >= {rows[half]['t0'][:10]})")


if __name__ == "__main__":
    main()
