"""Replay the fleet's REAL exit stack on the Phase-2 selector-study tokens.

Inputs: the study rows (scripts/selector_study.py) + price paths (scripts/selector_paths.py).
Exit logic is the live bot function bots.copy.trailing_stop.evaluate_exit_actions (stop, 4x/10x/
50x/1000x partial ladder, 45% multiplicative trailing after +20%), stepped bar by bar:
1-minute bars for the first 12h, 15-minute bars after that. Timeout closes at the last bar price.
Fee 1.25% per side on every fill. Fills at the bar price (a stop that gaps fills at the gapped
bar — realistic). NOT modelled: slippage beyond the fee, liquidity (a drained pool's frozen price
looks alive here), cluster's liquidity-momentum stop widening.

Variants are fixed up front (no tuning on this data):
  fleet     stop 8%,  timeout 12h   (cluster/conviction default stack)
  wide      stop 30%, timeout 12h   (cluster's deep-liquidity stop)
  swinglike stop 80%, timeout 7d
Entry delay: 1 min (webhook ~7s + fill) and 15 min (the study's assumption).

Usage: python scripts/selector_replay.py <study.jsonl> <paths.jsonl>
"""
import json
import random
import sys
from datetime import datetime

from bots.copy.trailing_stop import evaluate_exit_actions, PARTIAL_EXIT_TIERS

FEE = 0.0125
VARIANTS = {"fleet": (8.0, 12 * 3600), "wide": (30.0, 12 * 3600), "swinglike": (80.0, 7 * 86400)}


def path(p, t0u):
    """Merged (unix, price) path: 1m bars to t0+12h, then 15m bars."""
    m1 = [(u, x) for u, x in p["m1"] if x > 0]
    cut = t0u + 12 * 3600
    return m1 + [(u, x) for u, x in p["m15"] if x > 0 and u > max(cut, m1[-1][0] if m1 else 0)]


def replay(series, t0u, delay_s, stop, timeout_s):
    """Return per-trade return (fraction of cost) or None if no entry bar."""
    bars = [(u, x) for u, x in series if u >= t0u + delay_s]
    if len(bars) < 2:
        return None
    ent_u, entry = bars[0]
    remaining, proceeds, peak, done = 1.0, 0.0, None, ()
    last = entry
    for u, px in bars[1:]:
        if u > ent_u + timeout_s:
            break
        last = px
        peak, partials, close = evaluate_exit_actions(
            entry_price=entry, current_price=px, stored_peak_pct=peak,
            completed_tier_indexes=done, partial_tiers=PARTIAL_EXIT_TIERS, stop_pct=stop)
        for a in partials:
            sell = min(a.fraction, remaining)
            proceeds += sell * px / entry * (1 - FEE)
            remaining -= sell
            done = done + (a.tier_index,)
        if close or remaining <= 1e-9:
            break
    proceeds += remaining * last / entry * (1 - FEE)
    return proceeds / (1 + FEE) - 1


def boot_ci(xs, n=2000):
    random.seed(7)
    ms = sorted(sum(random.choices(xs, k=len(xs))) / len(xs) for _ in range(n))
    return ms[int(.025 * n)], ms[int(.975 * n)]


SUBSETS = {
    "ALL": lambda r: True,
    "launch age<0.25h": lambda r: (r["token_age_h"] or 0) < 0.25,
    "age>=1h": lambda r: (r["token_age_h"] or 0) >= 1,
    "age>=1h & promo_before": lambda r: (r["token_age_h"] or 0) >= 1 and r["promo_before"],
    "age>=1h & runup_from_low>=3": lambda r: (r["token_age_h"] or 0) >= 1 and (r.get("runup_from_low") or 0) >= 3,
    "age>=1h & sel_buy>=200": lambda r: (r["token_age_h"] or 0) >= 1 and (r["sel_buy_usd"] or 0) >= 200,
    "age>=1h & first_tracked>=60min": lambda r: (r["token_age_h"] or 0) >= 1 and r["mins_since_first_tracked"] >= 60,
    "age>=24h & flat 1h": lambda r: (r["token_age_h"] or 0) >= 24 and 0.8 <= (r.get("runup_1h") or 0) < 1.2,
    "runup_from_low>=10": lambda r: (r.get("runup_from_low") or 0) >= 10,
}


def main():
    study = {r["token"]: r for r in (json.loads(l) for l in open(sys.argv[1]) if l.strip()) if "hit2x_first" in r}
    res = []  # (row, {(variant, delay): ret})
    for l in open(sys.argv[2]):
        if not l.strip():
            continue
        p = json.loads(l)
        r = study.get(p["token"])
        if not r:
            continue
        t0u = int(datetime.fromisoformat(p["t0"]).timestamp())
        s = path(p, t0u)
        out = {}
        for v, (stop, to) in VARIANTS.items():
            for d in (60, 900):
                x = replay(s, t0u, d, stop, to)
                if x is not None:
                    out[(v, d)] = x
        res.append((r, out))
    res.sort(key=lambda t: t[0]["t0"])
    half = res[len(res) // 2][0]["t0"]
    print(f"tokens replayed: {len(res)}   halves split at t0 {half[:10]}")
    for v in VARIANTS:
        for d in (60, 900):
            print(f"\n=== {v}  (stop {VARIANTS[v][0]:g}%, timeout {VARIANTS[v][1] // 3600}h, entry +{d // 60}m) ===")
            print(f"{'subset':32} {'n':>5} {'mean':>7} {'95% CI':>17} {'median':>7} {'win%':>5} {'>=+100%':>7} {'H1 mean':>8} {'H2 mean':>8}")
            for name, f in SUBSETS.items():
                xs = [(r["t0"], o[(v, d)]) for r, o in res if (v, d) in o and f(r)]
                if len(xs) < 20:
                    continue
                vals = [x for _, x in xs]
                h1 = [x for t, x in xs if t < half]
                h2 = [x for t, x in xs if t >= half]
                lo, hi = boot_ci(vals)
                mean = sum(vals) / len(vals)
                print(f"{name:32} {len(vals):5} {mean:+7.1%} ({lo:+6.1%},{hi:+6.1%}) {sorted(vals)[len(vals) // 2]:+7.1%} "
                      f"{sum(x > 0 for x in vals) / len(vals):5.0%} {sum(x >= 1 for x in vals) / len(vals):7.1%} "
                      f"{(sum(h1) / len(h1) if h1 else 0):+8.1%} {(sum(h2) / len(h2) if h2 else 0):+8.1%}")


if __name__ == "__main__":
    main()
