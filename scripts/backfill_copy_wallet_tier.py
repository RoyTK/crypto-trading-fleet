"""Backfill COPY paper trades with wallet_tier metadata.

Bug context (2026-05-29): persist_paper_trade in bots/copy/loop_helpers.py
was not setting `sim_metadata->>'wallet_tier'`. The kill_criteria_monitor
filters on `sim_metadata->>'wallet_tier' = 'active'` so all 34 existing
trades were invisible to the kill criteria, silently letting COPY bleed
~$935 while the criteria showed N=0.

This script:
  1. Finds all COPY trades whose sim_metadata is missing wallet_tier
  2. Looks up the signal's payload->'wallets' for each
  3. Classifies the cluster tier (active iff ALL wallets are active-tier)
  4. Writes wallet_tier + cluster_wallets back into sim_metadata

Idempotent — safe to re-run. Skips trades already tagged.

Run on Hetzner:
    docker compose exec bot_copy python -m scripts.backfill_copy_wallet_tier

Output prints a summary table + the per-trade resolution.
"""
from __future__ import annotations

import json
import sys
from collections import Counter

from sqlalchemy import text

from framework.db import session_scope


def _classify(wallets: list[str], pool: dict[str, str]) -> str:
    if not wallets:
        return "unknown"
    if any(w not in pool for w in wallets):
        return "mixed"
    return "active" if all(pool[w] == "active" for w in wallets) else "mixed"


def main() -> int:
    print("\n=== COPY wallet_tier backfill ===\n", file=sys.stderr)

    with session_scope() as s:
        pool_rows = s.execute(text(
            "SELECT address, tier FROM wallet_pool"
        )).all()
        pool: dict[str, str] = {r.address: r.tier for r in pool_rows}
        print(f"[pool] {len(pool)} wallets loaded", file=sys.stderr)

        trade_rows = s.execute(text(
            """
            SELECT t.id, t.signal_id, t.sim_metadata, s.payload AS signal_payload
            FROM trades t LEFT JOIN signals s ON s.id = t.signal_id
            WHERE t.bot_id = 'copy' AND t.mode = 'paper'
            ORDER BY t.id ASC
            """
        )).all()
        print(f"[trades] {len(trade_rows)} COPY paper trades to evaluate", file=sys.stderr)

        tally: Counter[str] = Counter()
        updated = 0
        skipped_tagged = 0
        skipped_no_signal = 0

        for tr in trade_rows:
            sim_md = tr.sim_metadata or {}
            if isinstance(sim_md, str):
                sim_md = json.loads(sim_md)

            if sim_md.get("wallet_tier") is not None:
                skipped_tagged += 1
                continue

            if tr.signal_payload is None:
                skipped_no_signal += 1
                continue

            payload = tr.signal_payload
            if isinstance(payload, str):
                payload = json.loads(payload)
            wallets_payload = payload.get("wallets") or {}
            if isinstance(wallets_payload, dict):
                wallets = list(wallets_payload.keys())
            elif isinstance(wallets_payload, list):
                wallets = wallets_payload
            else:
                wallets = []

            tier = _classify(wallets, pool)
            tally[tier] += 1

            sim_md["wallet_tier"] = tier
            sim_md["cluster_wallets"] = wallets

            s.execute(
                text("UPDATE trades SET sim_metadata = :md WHERE id = :id"),
                {"md": json.dumps(sim_md), "id": tr.id},
            )
            updated += 1

    print(f"\n[done] updated={updated} skipped_already_tagged={skipped_tagged} "
          f"skipped_no_signal={skipped_no_signal}", file=sys.stderr)
    print(f"[tier distribution] {dict(tally)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
