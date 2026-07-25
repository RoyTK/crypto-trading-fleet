"""Team-follow team watch/promote-demote lifecycle (2026-07-24; chronic-demote + forward-only 2026-07-25).

Lifecycle (Roy's design — teamfollow is a MOONSHOT strategy: keep the fat tail, cut chronic bleed
at the TEAM level, not by over-raising the age floor):
  - active team trades live paper (strategy='teamfollow').
  - AUTO-DEMOTE to 'watch' if chronically negative: net <= demote_net_slow over >= min_trades_slow,
    OR net <= demote_net_fast over >= min_trades_fast (a fast bleeder demoted sooner). On demote the
    team's ACTIVE trades are RE-TAGGED 'teamfollow_watch' so the live number reflects only active teams.
  - watch team still fires the full gated entry but tags 'teamfollow_watch' (mode=paper, managed +
    measured, ISOLATED from live via _strategy_clause). It re-proves on FORWARD watch PnL (trades AFTER
    its demotion timestamp — its re-tagged history does NOT count against it; those losses were under
    the old regime). PROMOTE back to active if forward net > 0 over >= promote_min_trades.
  - (future) live gate: a proven-in-paper team graduates to live.

Status is DB-backed (teamfollow_team_status): survives autopull's git reset AND applies live (the entry
path reads it per fire — no bot restart).

Usage (bot_copy or framework container):
  python -m scripts.teamfollow_team_tiers report
  python -m scripts.teamfollow_team_tiers cycle                 # auto demote_chronic + promote (the daily cron)
  python -m scripts.teamfollow_team_tiers demote 44,20 "reason"
  python -m scripts.teamfollow_team_tiers promote [min_trades]
Read-only except demote/promote/cycle.
"""
import sys

from sqlalchemy import text

from framework.db import session_scope
from bots.copy.config import get_copy_settings
from bots.copy.loop_helpers import (
    ensure_team_status_table, set_teamfollow_team_status, list_teamfollow_team_status,
)


def _active_team_pnl() -> dict:
    """team_id -> (n, net) over CLOSED active 'teamfollow' trades."""
    out = {}
    with session_scope() as s:
        for team, n, net in s.execute(text("""
            SELECT (sim_metadata->>'team_id')::int, count(*), round(coalesce(sum(pnl_usd),0)::numeric,2)
            FROM trades WHERE bot_id='copy' AND mode='paper'
              AND sim_metadata->>'strategy'='teamfollow' AND exit_at IS NOT NULL
              AND sim_metadata->>'team_id' IS NOT NULL GROUP BY 1
        """)).all():
            out[int(team)] = (int(n), float(net))
    return out


def _watch_pnl() -> dict:
    """team_id -> {fwd:(n,net) after demotion ts, total:(n,net)} over CLOSED 'teamfollow_watch' trades.
    Forward = the re-prove window (its re-tagged history has entry_at < the demotion ts)."""
    out = {}
    with session_scope() as s:
        for team, fn, fnet, tn, tnet in s.execute(text("""
            SELECT (t.sim_metadata->>'team_id')::int,
                   count(*) FILTER (WHERE t.entry_at > s.updated_at),
                   round(coalesce(sum(t.pnl_usd) FILTER (WHERE t.entry_at > s.updated_at),0)::numeric,2),
                   count(*), round(coalesce(sum(t.pnl_usd),0)::numeric,2)
            FROM trades t
            JOIN teamfollow_team_status s
              ON (t.sim_metadata->>'team_id')::int = s.team_id AND s.status='watch'
            WHERE t.bot_id='copy' AND t.mode='paper'
              AND t.sim_metadata->>'strategy'='teamfollow_watch' AND t.exit_at IS NOT NULL
            GROUP BY 1, s.updated_at
        """)).all():
            out[int(team)] = {"fwd": (int(fn), float(fnet)), "total": (int(tn), float(tnet))}
    return out


def _retag_active_to_watch(team_id: int) -> int:
    """Move a team's active 'teamfollow' trades to 'teamfollow_watch' so the live number reflects
    only active teams. Returns rows moved."""
    with session_scope() as s:
        r = s.execute(text("""
            UPDATE trades
            SET sim_metadata = jsonb_set(sim_metadata::jsonb,'{strategy}','"teamfollow_watch"')::json,
                updated_at = now()
            WHERE bot_id='copy' AND mode='paper'
              AND sim_metadata->>'strategy'='teamfollow' AND (sim_metadata->>'team_id')::int = :t
        """), {"t": int(team_id)})
        return r.rowcount or 0


def demote(team_ids, reason: str) -> None:
    ensure_team_status_table()
    for t in team_ids:
        set_teamfollow_team_status(t, "watch", reason)      # sets updated_at = demotion boundary
        moved = _retag_active_to_watch(t)
        print(f"team {t} -> WATCH ({reason}) [{moved} active trades re-tagged to watch]")


def demote_chronic() -> list:
    """Auto-demote active teams that are chronically negative. Returns demoted team_ids."""
    ensure_team_status_table()
    cs = get_copy_settings()
    status = list_teamfollow_team_status()
    active_pnl = _active_team_pnl()
    demoted = []
    for team, (n, net) in active_pnl.items():
        if (status.get(team) or {}).get("status") == "watch":
            continue
        slow = n >= cs.copy_teamfollow_demote_min_trades_slow and net <= cs.copy_teamfollow_demote_net_slow
        fast = n >= cs.copy_teamfollow_demote_min_trades_fast and net <= cs.copy_teamfollow_demote_net_fast
        if slow or fast:
            tag = "fast-bleed" if fast else "chronic"
            demote([team], f"auto {tag}: ${net:.0f} over {n} active trades")
            demoted.append(team)
    if not demoted:
        print("no active teams hit the chronic-loss demote thresholds.")
    return demoted


def promote(min_trades: int) -> list:
    """Promote WATCH teams net-positive over >= min_trades FORWARD (post-demotion) watch trades."""
    ensure_team_status_table()
    status = list_teamfollow_team_status()
    wp = _watch_pnl()
    promoted = []
    for team, s in status.items():
        if s.get("status") != "watch":
            continue
        fn, fnet = wp.get(team, {}).get("fwd", (0, 0.0))
        if fn >= min_trades and fnet > 0:
            set_teamfollow_team_status(team, "active", f"re-proved: +${fnet:.2f} over {fn} forward watch trades")
            promoted.append((team, fnet, fn))
            print(f"team {team} -> ACTIVE (re-proved +${fnet:.2f} over {fn} forward watch trades)")
        else:
            print(f"team {team} stays WATCH (forward watch n={fn} net=${fnet:.2f}; need n>={min_trades} & net>0)")
    return promoted


def cycle() -> None:
    """The daily lifecycle: auto-demote chronic losers, then promote re-proven watch teams."""
    print("=== demote_chronic ===")
    demote_chronic()
    mt = get_copy_settings().copy_teamfollow_watch_promote_min_trades
    print(f"=== promote (forward watch net>0 over >={mt}) ===")
    promote(mt)


def report() -> None:
    ensure_team_status_table()
    status = list_teamfollow_team_status()
    active = _active_team_pnl()
    wp = _watch_pnl()
    teams = sorted(set(active) | set(status) | set(wp))
    print("team | status  | active(n,net)     | watch total(n,net) | watch FWD(n,net)   | reason")
    for t in teams:
        st = (status.get(t) or {}).get("status", "active")
        an, anet = active.get(t, (0, 0.0))
        wt = wp.get(t, {})
        tn, tnet = wt.get("total", (0, 0.0))
        fn, fnet = wt.get("fwd", (0, 0.0))
        reason = ((status.get(t) or {}).get("reason") or "")[:34]
        print(f"{t:>4} | {st:<7} | n={an:>2} ${anet:>8.2f} | n={tn:>2} ${tnet:>8.2f} | n={fn:>2} ${fnet:>8.2f} | {reason}")
    watch_ct = sum(1 for t in teams if (status.get(t) or {}).get("status") == "watch")
    print(f"\n{len(teams)} teams with data; {watch_ct} on watch.")


def main():
    if len(sys.argv) < 2:
        print("usage: report | cycle | demote <id,id,...> <reason> | promote [min_trades]")
        return
    cmd = sys.argv[1]
    if cmd == "report":
        report()
    elif cmd == "cycle":
        cycle()
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
