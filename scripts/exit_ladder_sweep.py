"""Exit-ladder redesign sweep (Roy 2026-07-18). The current ladder (4x/10x/50x/1000x)
has DEAD rungs — 50x/1000x never fire and 4x barely fires for wallet-copy, so those
strategies ride almost entirely on the multiplicative trailing. This tests candidate
ladders per strategy against the current one.

Model (peak/MFE based, path-light): for each closed trade we know peak_pct (MFE) and the
actual realized pnl. A candidate ladder sells frac_i of the position at each rung the peak
reached (locked at that multiple); the REMAINING fraction is valued via the multiplicative
trailing from the peak (peak_mult × (1 − giveback)) if the peak armed the trail (>= 20%),
else the trade's actual realized outcome (the stop/timeout — ladder-independent). This is an
approximation (assumes the trail triggered from the true peak) but it's consistent across
candidates, so the RANKING is meaningful. Read-only.
"""
from collections import defaultdict

from sqlalchemy import text
from framework.db import session_scope

ACTIVATION_PCT = 20.0
GIVEBACK = 0.45

# candidate ladders in peak_pct terms: [(threshold_pct, fraction), ...]
CANDIDATES = {
    "current 4x/10x/50x/1000x":   [(300, .25), (900, .25), (4900, .25), (99900, .25)],
    "trailing-only (no partials)": [],
    "light 2x(.20)":               [(100, .20)],
    "2x(.20)/4x(.25)/10x(.30)":    [(100, .20), (300, .25), (900, .30)],
    "1.5x(.20)/2x(.25)/3x(.25)":   [(50, .20), (100, .25), (200, .25)],
    "2x(.25)/3x(.25)/5x(.25)":     [(100, .25), (200, .25), (400, .25)],
}

SQL = """
SELECT regexp_replace(sim_metadata->>'strategy','_pre_reset.*|_interim.*','') AS fam,
  COALESCE((sim_metadata->>'peak_pct_since_entry')::float, 0) AS peak,
  size_usd, pnl_usd
FROM trades
WHERE bot_id='copy' AND mode='paper' AND fill_status='closed' AND size_usd > 0
"""


def ladder_pnl(peak, size, actual, tiers):
    remaining, proceeds = 1.0, 0.0
    for thr, frac in tiers:
        if peak >= thr:
            f = min(frac, remaining)
            proceeds += f * (1.0 + thr / 100.0) * size
            remaining -= f
            if remaining <= 1e-9:
                break
    if remaining > 1e-9:
        if peak >= ACTIVATION_PCT:
            rem_mult = (1.0 + peak / 100.0) * (1.0 - GIVEBACK)   # trailing from peak
        else:
            rem_mult = 1.0 + (actual / size)                     # actual (stop/timeout)
        proceeds += remaining * rem_mult * size
    return proceeds - size


def main():
    rows = defaultdict(list)
    with session_scope() as s:
        for r in s.execute(text(SQL)).mappings():
            rows[r["fam"]].append((float(r["peak"]), float(r["size_usd"]), float(r["pnl_usd"] or 0.0)))
    for fam in ("cluster", "teamfollow", "conviction", "promobuy"):
        data = rows.get(fam, [])
        if not data:
            continue
        recorded = sum(a for _, _, a in data)
        print(f"\n=== {fam}  (n={len(data)})   recorded PnL ${recorded:,.0f} ===")
        results = []
        for name, tiers in CANDIDATES.items():
            tot = sum(ladder_pnl(p, s, a, tiers) for p, s, a in data)
            results.append((name, tot))
        # 'current' is the calibration reference (its modeled value ≈ recorded if the model is sane)
        for name, tot in results:
            tag = "  <- MODEL≈recorded (calibration)" if name.startswith("current") else ""
            best = "  ** BEST **" if tot == max(r[1] for r in results) else ""
            print(f"    {name:<30} modeled ${tot:>+9,.0f}{best}{tag}")


if __name__ == "__main__":
    main()
