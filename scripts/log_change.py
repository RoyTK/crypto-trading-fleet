"""Record a strategy change in `strategy_changes` (dashboard markers + before/after tables).

Every deploy / config / roster change that affects a strategy should get a row, so its
effect can be judged on the trades that followed it.

Usage (inside bot_copy or framework container):
  python -m scripts.log_change --strategy cluster --kind roster \
      --desc "re-tier: pruned 174 snipers/MMs from active" [--at 2026-09-25T21:30Z] [--ref <sha>]
  python -m scripts.log_change --backfill      # idempotent; data-derived history
  python -m scripts.log_change --list

strategy: cluster|conviction|swing|teamfollow|cohortfire|promobuy|fleet
kind:     era_start|fix|config|roster|halt|other
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import text

from framework.db import session_scope

STRATEGIES = ("cluster", "conviction", "swing", "teamfollow", "cohortfire", "promobuy", "fleet")
KINDS = ("era_start", "fix", "config", "roster", "halt", "other")

_INSERT = text(
    "INSERT INTO strategy_changes (strategy, changed_at, kind, description, ref) "
    "VALUES (:s, :at, :k, :d, :r) ON CONFLICT (strategy, changed_at, description) DO NOTHING"
)


def add(strategy: str, kind: str, desc: str, at: datetime | None = None, ref: str | None = None) -> None:
    if strategy not in STRATEGIES or kind not in KINDS:
        raise SystemExit(f"bad strategy/kind: {strategy}/{kind}")
    with session_scope() as s:
        s.execute(_INSERT, {"s": strategy, "at": at or datetime.now(timezone.utc),
                            "k": kind, "d": desc, "r": ref})
        s.execute(text("COMMIT"))


def backfill() -> None:
    """Data-derived history only (no guessed dates)."""
    rows = []
    with session_scope() as s:
        # Era start of each strategy = first entry under its CURRENT tag (resets re-tag old trades).
        for st in ("cluster", "conviction", "swing", "teamfollow", "cohortfire", "promobuy"):
            at = s.execute(text(
                "SELECT min(entry_at) FROM trades WHERE bot_id='copy' AND mode='paper' "
                "AND sim_metadata->>'strategy'=:s"), {"s": st}).scalar()
            if at:
                rows.append((st, at, "era_start", f"{st} current era starts (first trade under current tag)", None))
        # Swing follow-out lookback fix 240h->2h: bot_copy restart that deployed 736dc41.
        at = s.execute(text(
            "SELECT created_at FROM audit_log WHERE event_type='bot_started' "
            "AND created_at BETWEEN '2026-09-08 20:50+00' AND '2026-09-08 20:55+00' "
            "ORDER BY created_at LIMIT 1")).scalar()
        if at:
            rows.append(("swing", at, "fix", "follow-out lookback 240h -> 2h", "736dc41"))
        # Re-tier: the single UPDATE that pruned the unfollowable wallets on 2026-09-25.
        r = s.execute(text(
            "SELECT demoted_at, count(*) n FROM wallet_pool WHERE tier='pruned' "
            "AND demoted_at >= '2026-09-25' AND demoted_at < '2026-09-26' "
            "GROUP BY demoted_at ORDER BY n DESC LIMIT 1")).first()
        if r and r.n >= 100:
            rows.append(("cluster", r.demoted_at, "roster",
                         f"re-tier: pruned {r.n} snipers/MMs from active pool", None))
    for st, at, k, d, ref in rows:
        add(st, k, d, at, ref)
        print(f"  {st:11} {at:%Y-%m-%d %H:%M} {k:9} {d}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy"); ap.add_argument("--kind", default="other")
    ap.add_argument("--desc"); ap.add_argument("--at"); ap.add_argument("--ref")
    ap.add_argument("--backfill", action="store_true"); ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.backfill:
        backfill()
    elif a.strategy and a.desc:
        at = datetime.fromisoformat(a.at.replace("Z", "+00:00")) if a.at else None
        add(a.strategy, a.kind, a.desc, at, a.ref)
        print("logged.")
    if a.list or a.backfill or (a.strategy and a.desc):
        with session_scope() as s:
            for r in s.execute(text("SELECT strategy, changed_at, kind, description FROM strategy_changes ORDER BY changed_at")):
                print(f"{r.strategy:11} {r.changed_at:%Y-%m-%d %H:%M} {r.kind:9} {r.description}")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
