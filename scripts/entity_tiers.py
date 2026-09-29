"""Per-strategy wallet/team watch lifecycle for conviction, swing and cohortfire (2026-09-29).

Generalises scripts/teamfollow_team_tiers.py (Roy: "apply the same strategy to all strategies
using wallets, including teams of wallets"). Each entity is judged ONLY on its own strategy's
current-era closed trades:
  entity = trigger wallet (conviction, swing) | cohort id (cohortfire)
  position = the entity's average trade size in that strategy
  DEMOTE to watch if  FAST: n >= copy_cull_min_trades_fast AND net <= -copy_cull_loss_positions_fast x position
                   or SLOW: n >= copy_cull_min_trades_slow AND net <= -copy_cull_loss_positions_slow x position
  On demote the entity's '<strategy>' trades are RE-TAGGED '<strategy>_watch' (the live number then
  reflects only entities in good standing) and new trades from it tag '<strategy>_watch' (isolated
  paper track). PROMOTE back when FORWARD watch net > 0 over >= copy_cull_promote_min_trades
  (forward = entered after the demotion; its re-tagged history does not count against it).
Status lives in strategy_entity_status (applies live — the entry path reads it per fire).
Teamfollow keeps its own job (same rule for its $500 positions); cluster is handled by the daily
wallet-pool job (wallet_pool_manager) on cluster-only attribution.

Usage (bot_copy container):
  python -m scripts.entity_tiers report [strategy]
  python -m scripts.entity_tiers cycle [--dry-run]        # daily cron
  python -m scripts.entity_tiers demote <strategy> <entity> "<reason>"
  python -m scripts.entity_tiers promote <strategy> <entity> "<reason>"
"""
import sys

from sqlalchemy import text

from framework.db import session_scope
from bots.copy.config import get_copy_settings
from bots.copy.loop_helpers import list_entity_status, set_entity_status

ENTITY = {
    "conviction": "sim_metadata->>'trigger_wallet'",
    "swing": "sim_metadata->>'trigger_wallet'",
    "cohortfire": "sim_metadata->>'team_id'",
}


def _active(strategy: str) -> dict:
    """entity -> (n, net, avg position) over CLOSED current-tag trades."""
    e = ENTITY[strategy]
    with session_scope() as s:
        return {str(r[0]): (int(r[1]), float(r[2]), float(r[3] or 0)) for r in s.execute(text(f"""
            SELECT {e}, count(*), coalesce(sum(pnl_usd), 0), avg(size_usd)
            FROM trades WHERE bot_id='copy' AND mode='paper' AND fill_status='closed'
              AND sim_metadata->>'strategy' = :s AND {e} IS NOT NULL GROUP BY 1"""),
            {"s": strategy})}


def _watch_forward(strategy: str) -> dict:
    """entity -> (n, net, avg position) over CLOSED '<strategy>_watch' trades entered AFTER its demotion."""
    e = ENTITY[strategy]
    with session_scope() as s:
        return {str(r[0]): (int(r[1]), float(r[2]), float(r[3] or 0)) for r in s.execute(text(f"""
            SELECT {e.replace('sim_metadata', 't.sim_metadata')}, count(*), coalesce(sum(t.pnl_usd), 0),
                   avg(t.size_usd)
            FROM trades t JOIN strategy_entity_status st
              ON st.strategy = :s AND st.status = 'watch'
             AND st.entity = {e.replace('sim_metadata', 't.sim_metadata')}
            WHERE t.bot_id='copy' AND t.mode='paper' AND t.fill_status='closed'
              AND t.sim_metadata->>'strategy' = :w AND t.entry_at > st.updated_at
            GROUP BY 1"""), {"s": strategy, "w": f"{strategy}_watch"})}


def _retag(strategy: str, entity: str) -> int:
    e = ENTITY[strategy]
    with session_scope() as s:
        r = s.execute(text(f"""
            UPDATE trades SET sim_metadata = jsonb_set(sim_metadata::jsonb, '{{strategy}}', to_jsonb(CAST(:w AS text)))::json,
                   updated_at = now()
            WHERE bot_id='copy' AND mode='paper' AND sim_metadata->>'strategy' = :s AND {e} = :ent"""),
            {"s": strategy, "w": f"{strategy}_watch", "ent": entity})
        s.execute(text("COMMIT"))
        return r.rowcount or 0


def demote(strategy: str, entity: str, reason: str) -> None:
    set_entity_status(strategy, entity, "watch", reason)
    moved = _retag(strategy, entity)
    print(f"  {strategy:10} {entity[:12]:12} -> WATCH ({reason}) [{moved} trades re-tagged {strategy}_watch]")


def _verdict(n: int, net: float, pos: float, cs):
    if pos <= 0:
        return None
    if n >= cs.copy_cull_min_trades_fast and net <= -cs.copy_cull_loss_positions_fast * pos:
        return "fast-bleed"
    if n >= cs.copy_cull_min_trades_slow and net <= -cs.copy_cull_loss_positions_slow * pos:
        return "chronic"
    return None


def cycle(dry_run: bool = False) -> None:
    cs = get_copy_settings()
    for strategy in ENTITY:
        status = list_entity_status(strategy)
        print(f"=== {strategy}: demote (fast n>={cs.copy_cull_min_trades_fast} & net<=-{cs.copy_cull_loss_positions_fast}x pos; "
              f"slow n>={cs.copy_cull_min_trades_slow} & net<=-{cs.copy_cull_loss_positions_slow}x pos) ===")
        hit = 0
        for ent, (n, net, pos) in sorted(_active(strategy).items(), key=lambda kv: kv[1][1]):
            if (status.get(ent) or {}).get("status") in ("watch", "retired"):
                continue
            v = _verdict(n, net, pos, cs)
            if v:
                hit += 1
                reason = f"auto {v}: ${net:.0f} over {n} trades (position ${pos:.0f})"
                if dry_run:
                    print(f"  [dry-run] {strategy:10} {ent[:12]:12} would -> WATCH ({reason})")
                else:
                    demote(strategy, ent, reason)
        if not hit:
            print("  none")
        mt = cs.copy_cull_promote_min_trades
        fwd = _watch_forward(strategy)
        for ent, st in status.items():
            if st.get("status") != "watch":
                continue
            fn, fnet, fpos = fwd.get(ent, (0, 0.0, 0.0))
            rv = _verdict(fn, fnet, fpos, cs)
            if rv:
                reason = f"retired: re-failed on watch ({rv}) ${fnet:.0f} over {fn} trades since cull"
                if dry_run:
                    print(f"  [dry-run] {strategy:10} {ent[:12]:12} would -> RETIRED ({reason})")
                else:
                    set_entity_status(strategy, ent, "retired", reason)
                    print(f"  {strategy:10} {ent[:12]:12} -> RETIRED ({reason})")
                continue
            if fn >= mt and fnet > 0:
                if dry_run:
                    print(f"  [dry-run] {strategy:10} {ent[:12]:12} would -> ACTIVE (+${fnet:.0f} over {fn} forward)")
                else:
                    set_entity_status(strategy, ent, "active", f"re-proved: +${fnet:.2f} over {fn} forward watch trades")
                    print(f"  {strategy:10} {ent[:12]:12} -> ACTIVE (re-proved +${fnet:.0f} over {fn})")


def report(only: str = None) -> None:
    for strategy in ([only] if only else ENTITY):
        status = list_entity_status(strategy)
        act, fwd = _active(strategy), _watch_forward(strategy)
        print(f"\n=== {strategy} ===  entity | status | active(n, net) | watch forward(n, net) | reason")
        for ent in sorted(set(act) | set(status), key=lambda e: act.get(e, (0, 0.0, 0))[1]):
            st = (status.get(ent) or {}).get("status", "active")
            n, net, _ = act.get(ent, (0, 0.0, 0))
            fn, fnet, _ = fwd.get(ent, (0, 0.0, 0.0))
            print(f"  {ent[:12]:12} | {st:6} | n={n:>3} ${net:>8.0f} | n={fn:>3} ${fnet:>7.0f} | "
                  f"{((status.get(ent) or {}).get('reason') or '')[:50]}")


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__); return
    if a[0] == "report":
        report(a[1] if len(a) > 1 else None)
    elif a[0] == "cycle":
        cycle(dry_run="--dry-run" in a)
    elif a[0] in ("demote", "promote") and len(a) >= 3:
        reason = a[3] if len(a) > 3 else f"manual {a[0]}"
        if a[0] == "demote":
            demote(a[1], a[2], reason)
        else:
            set_entity_status(a[1], a[2], "active", reason)
            print(f"{a[1]} {a[2]} -> ACTIVE ({reason})")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
