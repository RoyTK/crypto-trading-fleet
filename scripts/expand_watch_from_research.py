"""Expand the WATCH tier with bot-filtered research candidates (observe-only).

Roy 2026-07-10: Helius runs ~70% of the prepaid 10M plan, so use the headroom.
This subscribes promising *un-vetted* wallets from our own research tables to the
WATCH webhook so we collect live forward data on them — WITHOUT trading on them.

Safety (verified against wallet_pool_manager + wallet_pool_daily_cron):
  - WATCH is observe-only (webhook_receiver: no Redis publish for the watch tier).
  - Inserted with a NON-vetted source (not 'browser_opus*'), so decide_tier_changes
    with promote_vetted_only=True can NEVER auto-promote them to active/trading.
  - DROP_DAYS_SILENT=90 with a fresh-insert exemption → a 90-day observation window
    before a silent wallet is dropped; genuinely active accumulators keep firing
    events and persist. Dead wallets cost ~0 credits and age out on their own.
  - The daily cron only syncs Helius when there are tier TRANSITIONS; a plain
    insert isn't one, so this script calls the sync (_sync_helius) itself.

Candidate sources (both bot-filtered):
  1. recurrence_candidates — genuine (is_bot=false), status candidate/watching.
  2. prerun_accumulators — wallets that accumulated BEFORE a run, across >=2 distinct
     runners, avg lead >=0.25d (deliberate accumulation, not a launch snipe), >=$200
     committed, excluding any wallet flagged is_bot in recurrence_candidates.

Usage:
  docker compose exec -T framework python -m scripts.expand_watch_from_research --dry-run
  docker compose exec -T framework python -m scripts.expand_watch_from_research --limit 200
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import text

from framework.db import session_scope
from framework.logging_setup import configure_logging, get_logger
from bots.copy.config import get_copy_settings

log = get_logger("expand_watch")

# Ranked union of the two research sources; recurrence-genuine outrank prerun.
SELECT_CANDIDATES = text("""
WITH pool AS (SELECT address FROM wallet_pool),
bots AS (SELECT wallet FROM recurrence_candidates WHERE is_bot IS TRUE),
rec_genuine AS (
    SELECT wallet,
           2000 + COALESCE(peak_runners, 0) AS score,
           'recurrence_watch' AS source
    FROM recurrence_candidates
    WHERE COALESCE(is_bot, FALSE) = FALSE
      AND status IN ('candidate', 'watching')
      AND wallet NOT IN (SELECT address FROM pool)
),
prerun_genuine AS (
    SELECT pa.wallet,
           1000
             + 50 * COUNT(DISTINCT pa.token)
             + LEAST(COALESCE(AVG(pa.lead_days), 0), 10) * 3 AS score,
           'prerun_watch' AS source
    FROM prerun_accumulators pa
    WHERE pa.wallet NOT IN (SELECT address FROM pool)
      AND pa.wallet NOT IN (SELECT wallet FROM bots)
    GROUP BY pa.wallet
    HAVING COUNT(DISTINCT pa.token) >= 2
       AND AVG(COALESCE(pa.lead_days, 0)) >= 0.25
       AND SUM(COALESCE(pa.bought_usd, 0)) >= 200
),
combined AS (
    SELECT wallet, score, source FROM rec_genuine
    UNION ALL
    SELECT wallet, score, source FROM prerun_genuine
)
SELECT wallet,
       MAX(score) AS score,
       -- prefer the recurrence provenance label when a wallet is in both
       CASE WHEN bool_or(source = 'recurrence_watch') THEN 'recurrence_watch'
            ELSE 'prerun_watch' END AS source
FROM combined
GROUP BY wallet
ORDER BY score DESC
LIMIT :limit
""")

INSERT_WATCH = text("""
INSERT INTO wallet_pool (address, chain, tier, source, added_at)
VALUES (:addr, 'solana', 'watch', :source, now())
ON CONFLICT (address) DO NOTHING
""")


def main() -> int:
    configure_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200,
                    help="max wallets to add to the watch tier")
    ap.add_argument("--dry-run", action="store_true",
                    help="preview the selection; do not insert or sync")
    args = ap.parse_args()

    with session_scope() as s:
        rows = s.execute(SELECT_CANDIDATES, {"limit": args.limit}).all()

    by_src = {}
    for r in rows:
        by_src[r.source] = by_src.get(r.source, 0) + 1
    print(f"[expand_watch] selected {len(rows)} candidates "
          f"(recurrence={by_src.get('recurrence_watch', 0)} "
          f"prerun={by_src.get('prerun_watch', 0)}); limit={args.limit}")
    for r in rows[:8]:
        print(f"    {r.wallet[:16]}…  score={r.score:.0f}  {r.source}")
    if len(rows) > 8:
        print(f"    … +{len(rows) - 8} more")

    if args.dry_run:
        print("[expand_watch] DRY RUN — nothing inserted.")
        return 0
    if not rows:
        print("[expand_watch] no candidates — nothing to do.")
        return 0

    inserted = 0
    with session_scope() as s:
        for r in rows:
            res = s.execute(INSERT_WATCH, {"addr": r.wallet, "source": r.source})
            inserted += res.rowcount or 0
        watch_total = int(s.execute(text(
            "SELECT COUNT(*) FROM wallet_pool WHERE chain='solana' AND tier='watch'"
        )).scalar() or 0)
    print(f"[expand_watch] inserted {inserted} new watch wallets "
          f"(watch tier now {watch_total})")
    log.info("watch_expanded", inserted=inserted, watch_total=watch_total)

    # Sync the WATCH webhook (the daily cron won't — it only syncs on transitions).
    settings = get_copy_settings()
    api_key = settings.helius_api_key
    auth_secret = os.environ.get("HELIUS_WEBHOOK_AUTH_SECRET", "")
    active_url = os.environ.get("COPY_WEBHOOK_URL", "")
    watch_url = os.environ.get("COPY_WEBHOOK_URL_WATCH", "")
    if not (api_key and auth_secret and active_url and watch_url):
        print("[expand_watch] WARNING: Helius env incomplete — inserted but NOT synced. "
              "Run wallet_pool_daily_cron (with a transition) or re-run when env is set.")
        return 0

    from scripts.wallet_pool_daily_cron import _sync_helius
    try:
        res = asyncio.run(_sync_helius(api_key, auth_secret, active_url, watch_url))
        print(f"[expand_watch] helius synced: active={res['active'][1]}, "
              f"watch={res['watch'][1]}")
        log.info("watch_synced", **{k: v[1] for k, v in res.items()})
    except Exception as e:
        log.exception("watch_sync_failed")
        print(f"[expand_watch] SYNC FAILED: {e} — rows inserted; retry the sync.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
