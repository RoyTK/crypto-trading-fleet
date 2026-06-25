"""Read-only preview: how often would the conviction roster fire?

Phase-1 sizing check (project plan 2026-06-24) BEFORE enabling the conviction
strategy. Answers "will this even get enough signals?" by counting recent
webhook events for the roster wallets.

DATA CAVEAT: wallet_events_log records (wallet_address, event_at, source_webhook)
only — NOT buy/sell direction or notional. So these counts are an UPPER BOUND
on conviction triggers: actual triggers = BUYS >= the per-wallet notional floor
(copy_conviction_min_notional_usd), a fraction of total events. Also, only
ACTIVE-tier wallets emit to the copy:buys channel the bot consumes, so non-active
roster members contribute zero real triggers regardless of their event count.

Run (inside the framework container):
  python -m scripts.conviction_signal_preview                # roster from DB, 30d
  python -m scripts.conviction_signal_preview --days 14
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from bots.copy.config import get_copy_settings
from framework.db import session_scope


def main() -> int:
    ap = argparse.ArgumentParser(description="Preview conviction signal frequency.")
    ap.add_argument("--days", type=int, default=30, help="lookback window in days (default 30)")
    args = ap.parse_args()
    days = max(1, args.days)

    settings = get_copy_settings()
    floor = settings.copy_conviction_min_notional_usd

    with session_scope() as s:
        roster = s.execute(text(
            "SELECT address, tier FROM wallet_pool WHERE conviction = true ORDER BY address"
        )).all()
        if not roster:
            print("conviction roster is EMPTY — flag wallets with "
                  "scripts/set_conviction_wallets.py first.")
            return 0

        addrs = [r.address for r in roster]
        tier_by_addr = {r.address: r.tier for r in roster}
        active = [a for a in addrs if tier_by_addr.get(a) == "active"]

        rows = s.execute(text(
            """
            SELECT wallet_address, COUNT(*) AS n
            FROM wallet_events_log
            WHERE wallet_address = ANY(:addrs)
              AND event_at >= NOW() - (:days || ' days')::interval
            GROUP BY wallet_address
            """
        ), {"addrs": addrs, "days": str(days)}).all()
        counts = {r.wallet_address: int(r.n) for r in rows}

    total_events = sum(counts.values())
    active_events = sum(counts.get(a, 0) for a in active)

    print(f"Conviction signal-frequency preview — last {days} days")
    print(f"Per-wallet notional floor: ${floor:,.0f}  (triggers = buys above this)")
    print(f"Roster: {len(addrs)} wallets ({len(active)} active-tier)\n")
    print(f"{'wallet':<46} {'tier':<8} {'events':>8} {'/day':>7}")
    print("-" * 72)
    for a in sorted(addrs, key=lambda x: counts.get(x, 0), reverse=True):
        n = counts.get(a, 0)
        per_day = n / days
        tier = tier_by_addr.get(a, "?")
        mark = "" if tier == "active" else "  (no triggers — not active)"
        print(f"{a:<46} {tier:<8} {n:>8} {per_day:>7.1f}{mark}")

    print("-" * 72)
    print(f"TOTAL events: {total_events}  (~{total_events/days:.1f}/day)")
    print(f"ACTIVE-tier events: {active_events}  (~{active_events/days:.1f}/day) "
          f"<- the only ones that can trigger")
    print("\nNOTE: these are ALL webhook events (buys + sells). Actual conviction "
          f"triggers are BUYS >= ${floor:,.0f} on distinct tokens — a fraction of "
          "the above. Use this to judge whether the roster is active enough to be "
          "worth enabling, not as an exact trade-count forecast.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
