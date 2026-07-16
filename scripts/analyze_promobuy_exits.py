"""Promo-buy EXIT-STRATEGY analysis — MFE-based take-profit sweep, segmented by source + track.

Model: a fixed take-profit at X% = sell 100% of the position at +X% IF the trade's MFE
(peak_pct_since_entry) reached X (we'd have caught it on the way up); otherwise the trade keeps
its ACTUAL realized outcome (downside held constant at the current stop). Sweeping X finds the TP
that maximizes total $, and segmenting by source (profile/boost) x track (has-liq/null-liq) shows
where the edge is + whether different exits per segment help. Compares every TP to the CURRENT
ladder (baseline) and to a perfect sell-at-peak ceiling. Read-only; run server-side.

CAVEAT: a single fixed TP can't do partials, so for a moonshot-driven segment the current
partial+trailing ladder (baseline) may beat any fixed TP — that itself is the answer for that
segment. For a mostly-rugging segment, an early TP that banks the occasional peak may beat it.
"""
from collections import Counter
from sqlalchemy import text
from framework.db import session_scope

TP_LEVELS = [40, 50, 60, 70, 80, 90, 100, 120, 150, 200, 300, 500]

SQL = """
SELECT t.id, t.fill_status, t.size_usd,
  COALESCE((t.sim_metadata->>'peak_pct_since_entry')::float, 0) AS peak,
  t.pnl_usd,
  (t.sim_metadata->>'entry_liquidity_usd') AS entry_liq,
  sf.features_json->>'source' AS source,
  (SELECT COALESCE(SUM((e->>'received_usdc')::numeric - t.size_usd*(e->>'fraction')::numeric),0)
   FROM json_array_elements(t.sim_metadata->'partial_exits') e WHERE e->>'status'='filled') AS open_realized
FROM trades t LEFT JOIN signal_features sf ON sf.trade_id = t.id
WHERE t.bot_id='copy' AND t.mode='paper' AND t.sim_metadata->>'strategy' LIKE 'promobuy%'
"""


def load():
    rows = []
    with session_scope() as s:
        for r in s.execute(text(SQL)).mappings():
            size = float(r["size_usd"] or 0)
            actual = float(r["pnl_usd"] or 0) if r["fill_status"] == "closed" else float(r["open_realized"] or 0)
            rows.append(dict(
                track="null-liq" if r["entry_liq"] is None else "has-liq",
                src=(r["source"] or "unknown"),
                size=size, peak=float(r["peak"] or 0), actual=actual,
                status=r["fill_status"]))
    return rows


def report(label, rows):
    if not rows:
        print(f"\n=== {label}: (no trades) ===")
        return
    n = len(rows)
    baseline = sum(r["actual"] for r in rows)                    # current ladder
    at_peak = sum(r["peak"] / 100 * r["size"] for r in rows)     # perfect-exit ceiling
    curve = {}
    for X in TP_LEVELS:
        prof = sum((X / 100 * r["size"]) if r["peak"] >= X else r["actual"] for r in rows)
        hits = sum(1 for r in rows if r["peak"] >= X)
        curve[X] = (prof, hits)
    bestX, (bestP, bestH) = max(curve.items(), key=lambda kv: kv[1][0])
    verdict = ("fixed-TP beats current ladder" if bestP > baseline + 1
               else "current ladder wins (keep it)")
    print(f"\n=== {label}  (n={n})")
    print(f"    current ladder net = ${baseline:>+9,.0f}   |   sell-at-peak ceiling = ${at_peak:>+9,.0f}")
    for X, (prof, hits) in curve.items():
        mark = "  <== BEST TP" if X == bestX else ""
        print(f"    TP@{X:>3}% -> ${prof:>+9,.0f}   ({hits}/{n} reached +{X}%){mark}")
    print(f"    >>> best fixed TP = +{bestX}% (${bestP:,.0f}); baseline ${baseline:,.0f}  =>  {verdict}")


rows = load()
print(f"loaded {len(rows)} promobuy trades (all eras)")
print("segment coverage (track,source):", dict(Counter((r["track"], r["src"]) for r in rows)))

report("ALL promobuy", rows)
report("HAS-LIQ (confirmable liquidity)", [r for r in rows if r["track"] == "has-liq"])
report("NULL-LIQ (bonding-curve)", [r for r in rows if r["track"] == "null-liq"])
for src in ("profile", "boost"):
    report(f"SOURCE = {src}", [r for r in rows if r["src"] == src])
for track in ("has-liq", "null-liq"):
    for src in ("profile", "boost"):
        report(f"{track.upper()} x {src}", [r for r in rows if r["track"] == track and r["src"] == src])
