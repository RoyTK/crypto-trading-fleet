"""Add an explicit wallet list to the WATCH tier (observe-only) + sync Helius.

Generic sibling of expand_watch_from_research.py for curated lists (e.g. the
RED-COHORT top-tier sniper-cohort wallets, research/redcohort_top81_wallets.json).

Safety (same contract as expand_watch_from_research):
  - WATCH is observe-only (webhook_receiver never publishes watch events to Redis).
  - `source` must NOT start with 'browser_opus' → promote_vetted_only can never
    auto-promote these wallets to trading.
  - 90d silent-drop grace applies from added_at.
  - Syncs the watch webhook itself (the daily cron only syncs on transitions).

Usage:
  docker compose exec -T framework python -m scripts.add_watch_wallets \
      --file research/redcohort_top81_wallets.json --source redcohort_watch [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from sqlalchemy import text

from framework.db import session_scope
from framework.logging_setup import configure_logging, get_logger
from bots.copy.config import get_copy_settings

log = get_logger("add_watch_wallets")

INSERT = text("""
INSERT INTO wallet_pool (address, chain, tier, source, added_at)
VALUES (:addr, 'solana', 'watch', :source, now())
ON CONFLICT (address) DO NOTHING
""")


def main() -> int:
    configure_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="JSON with a 'wallets' list (or a bare list)")
    ap.add_argument("--source", required=True, help="wallet_pool.source tag (non-browser_opus)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.source.lower().startswith("browser_opus"):
        print("refusing: source must not start with browser_opus (would be auto-promotable)")
        return 1

    data = json.load(open(args.file))
    wallets = data["wallets"] if isinstance(data, dict) else data
    wallets = sorted(set(wallets))
    print(f"[add_watch] {len(wallets)} wallets from {args.file} (source={args.source})")

    if args.dry_run:
        print("[add_watch] DRY RUN — nothing inserted.")
        return 0

    inserted = 0
    with session_scope() as s:
        for w in wallets:
            res = s.execute(INSERT, {"addr": w, "source": args.source})
            inserted += res.rowcount or 0
        watch_total = int(s.execute(text(
            "SELECT COUNT(*) FROM wallet_pool WHERE chain='solana' AND tier='watch'"
        )).scalar() or 0)
    print(f"[add_watch] inserted {inserted} new (rest already in pool); watch tier now {watch_total}")
    log.info("watch_added", inserted=inserted, watch_total=watch_total, source=args.source)

    settings = get_copy_settings()
    api_key = settings.helius_api_key
    auth = os.environ.get("HELIUS_WEBHOOK_AUTH_SECRET", "")
    active_url = os.environ.get("COPY_WEBHOOK_URL", "")
    watch_url = os.environ.get("COPY_WEBHOOK_URL_WATCH", "")
    if not (api_key and auth and active_url and watch_url):
        print("[add_watch] WARNING: Helius env incomplete — inserted but NOT synced.")
        return 0
    from scripts.wallet_pool_daily_cron import _sync_helius
    res = asyncio.run(_sync_helius(api_key, auth, active_url, watch_url))
    print(f"[add_watch] helius synced: active={res['active'][1]}, watch={res['watch'][1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
