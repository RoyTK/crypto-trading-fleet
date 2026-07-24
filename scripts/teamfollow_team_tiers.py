"""Team-follow team watch/promote-demote lifecycle (2026-07-24).

A demoted team is set status='watch' in the DB table teamfollow_team_status. A watch team
still fires + runs the FULL gated entry, but its trades are tagged strategy='teamfollow_watch'
(mode=paper, fully managed + measured, but ISOLATED from the live teamfollow bankroll/metrics/
dashboards via _strategy_clause). It re-proves itself on FORWARD PnL: promote() returns any
watch team that is net-positive over >= promote_min_trades closed watch trades back to 'active'.

Status is DB-backed on purpose: a roster-JSON edit would be wiped by autopull's git reset, and
DB status also applies LIVE (the entry path reads it per fire — no bot restart needed).

Usage (run in the framework or bot_copy container):
  docker compose exec -T bot_copy python -m scripts.teamfollow_team_tiers report
  docker compose exec -T bot_copy python -m scripts.teamfollow_team_tiers demote 44,20,84,112 "workflow 2026-07-24: net-negative, re-prove on watch"
  docker compose exec -T bot_copy python -m scripts.teamfollow_team_tiers promote [min_trades]
Read-only except demote/promote, which upsert the status table.
"""
import sys

from sqlalchemy import text

from framework.db import session_scope
from bots.copy.config import get_copy_settings
from bots.copy.loop_helpers import (
    ensure_team_status_table, set_teamfollow_team_status, list_teamfollow_team_status,
)


def _team_pnl() -> dict:
    """team_id -> {active: {n, net}, watch: {n, net}} over CLOSED paper trades."""
    out: dict = {}
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT (sim_metadata->>'team_id')::int AS team,
                   sim_metadata->>'strategy' AS strat,
                   count(*) AS n,
                   round(coalesce(sum(pnl_usd),0)::numeric, 2) AS net
            FROM trades
            WHERE bot_id='copy' AND mode='paper' AND exit_at IS NOT NULL
              AND sim_metadata->>'strategy' IN ('teamfollow','teamfollow_watch')
              AND sim_metadata->>'team_id' IS NOT NULL
            GROUP BY 1,2
        """)).all()
    for team, strat, n, net in rows:
        d = out.setdefault(team, {"active": {"n": 0, "net": 0.0}, "watch": {"n": 0, "net": 0.0}})
        key = "watch" if strat == "teamfollow_watch" else "active"
        d[key] = {"n": int(n), "net": float(net)}
    return out


def report() -> None:
    ensure_team_status_table()
    status = list_teamfollow_team_status()
    pnl = _team_pnl()
    teams = sorted(set(pnl) | set(status))
    print("team | status  | active(n,net)      | watch(n,net)       | reason")
    for t in teams:
        st = (status.get(t) or {}).get("status", "active")
        a = pnl.get(t, {}).get("active", {"n": 0, "net": 0.0})
        w = pnl.get(t, {}).get("watch", {"n": 0, "net": 0.0})
        reason = (status.get(t) or {}).get("reason", "") or ""
        print(f"{t:>4} | {st:<7} | n={a['n']:>2} ${a['net']:>9.2f} | n={w['n']:>2} ${w['net']:>9.2f} | {reason[:40]}")
    watch_ct = sum(1 for t in teams if (status.get(t) or {}).get("status") == "watch")
    print(f"\n{len(teams)} teams with data; {watch_ct} on watch.")


def demote(team_ids: list[int], reason: str) -> None:
    ensure_team_status_table()
    for t in team_ids:
        set_teamfollow_team_status(t, "watch", reason)
        print(f"team {t} -> WATCH ({reason})")


def promote(min_trades: int) -> None:
    """Promote any WATCH team net-positive over >= min_trades closed teamfollow_watch trades."""
    ensure_team_status_table()
    status = list_teamfollow_team_status()
    pnl = _team_pnl()
    promoted = []
    for t, s in status.items():
        if s.get("status") != "watch":
            continue
        w = pnl.get(t, {}).get("watch", {"n": 0, "net": 0.0})
        if w["n"] >= min_trades and w["net"] > 0:
            set_teamfollow_team_status(t, "active", f"re-proved: +${w['net']:.2f}/{w['n']} watch trades")
            promoted.append((t, w["net"], w["n"]))
            print(f"team {t} -> ACTIVE (re-proved +${w['net']:.2f} over {w['n']} watch trades)")
        else:
            print(f"team {t} stays WATCH (watch n={w['n']} net=${w['net']:.2f}; need n>={min_trades} & net>0)")
    if not promoted:
        print("no teams promoted this run.")


def main():
    if len(sys.argv) < 2:
        print("usage: report | demote <id,id,...> <reason> | promote [min_trades]")
        return
    cmd = sys.argv[1]
    if cmd == "report":
        report()
    elif cmd == "demote":
        ids = [int(x) for x in sys.argv[2].split(",") if x.strip()]
        reason = sys.argv[3] if len(sys.argv) > 3 else "manual demote"
        demote(ids, reason)
    elif cmd == "promote":
        mt = int(sys.argv[2]) if len(sys.argv) > 2 else get_copy_settings().copy_teamfollow_watch_promote_min_trades
        promote(mt)
    else:
        print("unknown command:", cmd)


if __name__ == "__main__":
    main()
