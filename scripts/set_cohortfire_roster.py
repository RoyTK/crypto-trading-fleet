"""Keep the cohortfire roster subscribed and protected (2026-09-29).

cohortfire acts on buys from the wallets in bots/copy/cohortfire_roster.json. Those wallets are
launch snipers, so every generic pool process (cluster vetting, the daily pool cron, the 09-25
re-tier) rejects them — on 2026-08-03 a backlog vet pruned 48 of the 81 and silenced cohortfire
for 8 weeks. This script makes the roster self-consistent with the pool:
  * a roster wallet that is pruned or missing -> tier 'watch' (Helius-subscribed), demoted_at NULL
  * every roster wallet in 'watch' -> pinned=true, pinned_reason='cohortfire roster'
    (pinned = immune to the daily cron, the vetting REJECT path and the re-tier)
  * roster wallets in 'active' / 'teamfollow' are left as they are (the webhook receiver forwards
    cohort buys from any tier) and NOT pinned (their own strategy's cull must still apply)
  * pool wallets pinned 'cohortfire roster' that are no longer in the roster -> unpinned
Cohort-level culling is NOT done here — that's scripts/entity_tiers.py (cohort -> cohortfire_watch).

After changing the roster FILE: commit + push, then restart webhook_receiver and bot_copy (both
load the roster at startup). Run (framework container, has the Helius env):
  python -m scripts.set_cohortfire_roster [--dry-run] [--no-sync-helius]
"""
import json
import sys
from pathlib import Path

from sqlalchemy import text

from framework.db import session_scope

REASON = "cohortfire roster"
ROSTER = Path(__file__).resolve().parent.parent / "bots" / "copy" / "cohortfire_roster.json"


def main():
    dry = "--dry-run" in sys.argv
    roster = set((json.loads(ROSTER.read_text()).get("wallets") or {}).keys())
    restored = pinned = unpinned = 0
    with session_scope() as s:
        pool = {r.address: r for r in s.execute(text(
            "SELECT address, tier, pinned, pinned_reason FROM wallet_pool WHERE address = ANY(:a) "
            "OR pinned_reason = :r"), {"a": list(roster), "r": REASON})}
        for a in sorted(roster):
            w = pool.get(a)
            if w is None or w.tier == "pruned":
                restored += 1
                print(f"  restore {a[:14]}…  {'missing' if w is None else 'pruned'} -> watch (pinned)")
                if not dry:
                    s.execute(text("""
                        INSERT INTO wallet_pool (address, chain, tier, source, added_at, pinned, pinned_at, pinned_reason)
                        VALUES (:a, 'solana', 'watch', 'redcohort_watch', now(), true, now(), :r)
                        ON CONFLICT (address) DO UPDATE SET tier='watch', demoted_at=NULL,
                          pinned=true, pinned_at=now(), pinned_reason=:r"""), {"a": a, "r": REASON})
            elif w.tier == "watch" and not w.pinned:
                pinned += 1
                print(f"  pin     {a[:14]}…  watch")
                if not dry:
                    s.execute(text("UPDATE wallet_pool SET pinned=true, pinned_at=now(), pinned_reason=:r "
                                   "WHERE address=:a"), {"a": a, "r": REASON})
        for a, w in pool.items():
            if a not in roster and w.pinned_reason == REASON:
                unpinned += 1
                print(f"  unpin   {a[:14]}…  no longer in roster (tier {w.tier})")
                if not dry:
                    s.execute(text("UPDATE wallet_pool SET pinned=false, pinned_reason=NULL WHERE address=:a"),
                              {"a": a})
        if not dry:
            s.execute(text("COMMIT"))
    print(f"\n{'would apply' if dry else 'applied'}: {restored} restored, {pinned} pinned, "
          f"{unpinned} unpinned (roster {len(roster)})")
    if not dry and restored and "--no-sync-helius" not in sys.argv:
        from scripts.apply_vetting_results import _sync_helius_from_db
        _sync_helius_from_db()


if __name__ == "__main__":
    main()
