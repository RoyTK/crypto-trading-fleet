"""Bot-farm detector (2026-10-03) — find tracked wallets that are really one operator.

A pair (A, B) is linked when >= MIN_JOINT_PCT of A's buys AND of B's buys land within WINDOW_S
seconds of the other's buy of the same token, over >= MIN_JOINT buys in the lookback. Linked
wallets are grouped (connected components) and written to `wallet_bundle` (rebuilt each run).

Why: one operator's wallets can fake agreement — a 3-wallet "cluster" with no independent buyer
(34 cluster trades, −$953 through 10-03), and each wallet triggers swing separately (bundle A:
21 swing trades, −$1,779). Evidence on the 4 suspects: A/B/C bought within 2s of a partner on
87–100% of buys; D (a cluster-only coincidence) on 0–4%. Raw buy timing, not co-occurrence in
our trades, is what separates them.

REPORT ONLY (Roy 2026-10-03): shown on the Wallet Pool page; nothing trades or culls on it. A
profitable bundle (e.g. the cluster trio HZXdw/yMBR/7Dv2) is kept.

Run (framework container): python -m scripts.bot_farm_detect [--dry-run]   (daily cron 07:15 UTC)
"""
import sys

from sqlalchemy import text

from framework.db import session_scope

LOOKBACK_DAYS = 14
WINDOW_S = 2
MIN_JOINT_PCT = 90.0
MIN_JOINT = 20

PAIRS_SQL = f"""
WITH p AS (
  SELECT b1.wallet_address a, b2.wallet_address b, count(DISTINCT b1.id) joint
  FROM wallet_swaps_log b1 JOIN wallet_swaps_log b2
    ON b2.token_mint = b1.token_mint AND b2.side = 'buy' AND b2.wallet_address <> b1.wallet_address
   AND b2.event_at BETWEEN b1.event_at - interval '{WINDOW_S} seconds' AND b1.event_at + interval '{WINDOW_S} seconds'
  WHERE b1.side = 'buy' AND b1.event_at > now() - interval '{LOOKBACK_DAYS} days'
  GROUP BY 1, 2),
n AS (SELECT wallet_address w, count(*) buys FROM wallet_swaps_log
      WHERE side = 'buy' AND event_at > now() - interval '{LOOKBACK_DAYS} days' GROUP BY 1)
SELECT p.a, p.b, p.joint, n.buys, 100.0 * p.joint / n.buys AS pct
FROM p JOIN n ON n.w = p.a
WHERE p.joint >= {MIN_JOINT}
"""


def detect():
    with session_scope() as s:
        rows = s.execute(text(PAIRS_SQL)).all()
    pct = {(r.a, r.b): float(r.pct) for r in rows}
    buys = {r.a: int(r.buys) for r in rows}
    # a link needs BOTH directions >= MIN_JOINT_PCT (one busy wallet shadowing a quiet one is not a farm)
    links = [(a, b) for (a, b), v in pct.items() if a < b and v >= MIN_JOINT_PCT
             and pct.get((b, a), 0.0) >= MIN_JOINT_PCT]
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in links:
        parent[find(a)] = find(b)
    groups = {}
    for w in parent:
        groups.setdefault(find(w), []).append(w)
    best = {}
    for (a, b), v in pct.items():
        if a in parent and b in parent and find(a) == find(b):
            best[a] = max(best.get(a, 0.0), v)
    out = []
    for gid, members in enumerate(sorted(groups.values(), key=lambda m: -len(m)), 1):
        for w in sorted(members):
            out.append((w, gid, len(members), buys.get(w, 0), round(best.get(w, 0.0), 1)))
    return out


def main():
    dry = "--dry-run" in sys.argv
    out = detect()
    n_groups = len({g for _, g, *_ in out})
    print(f"bot-farm detector: {n_groups} groups, {len(out)} wallets "
          f"(>= {MIN_JOINT_PCT:.0f}% of buys within {WINDOW_S}s of a partner, {LOOKBACK_DAYS}d)")
    for w, g, size, b, p in out:
        print(f"  group {g:3} (size {size})  {w}  buys={b}  joint={p}%")
    if dry:
        return
    with session_scope() as s:
        s.execute(text("DELETE FROM wallet_bundle"))
        for w, g, size, b, p in out:
            s.execute(text("INSERT INTO wallet_bundle (address, bundle_id, bundle_size, buys, joint_pct, detected_at) "
                           "VALUES (:w, :g, :n, :b, :p, now())"), {"w": w, "g": g, "n": size, "b": b, "p": p})
        s.execute(text("COMMIT"))
    print("wallet_bundle rebuilt")


if __name__ == "__main__":
    main()
