"""One-shot migration: populate structure_whale_pool table from whale_list.json.

Runs once on Hetzner after alembic 0004 upgrade. Reads current
bots/structure/whale_list.json and inserts each entry into the new
structure_whale_pool table preserving all curation metrics + tier
classification.

After this runs successfully:
  1. bot_structure (next restart) reads pool from DB instead of JSON
  2. whale_pool_growth.py + whale_graduation_scan.py write to DB
  3. whale_list.json becomes read-only / archival
  4. git operations can no longer wipe the pool

Idempotent: skips addresses already in structure_whale_pool. Safe to
re-run after any new whales are added to whale_list.json by other tooling.

Run:
    docker compose exec -T framework python -m scripts.structure_whale_pool_migrate_initial
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from framework.audit import write_audit
from framework.db import session_scope
from framework.logging_setup import configure_logging, get_logger
from framework.models import StructureWhalePool


log = get_logger("structure_whale_pool_migrate_initial")

WHALE_LIST_PATH = Path("bots/structure/whale_list.json")


def _tier_from_entry(entry: dict) -> str:
    """Map whale_list.json `tier` field (or absence) to DB tier."""
    raw = (entry.get("tier") or "").strip()
    # Old format used '$5M_$500k' / '$2M_$250k'. Untagged → working.
    if raw == "$5M_$500k":
        return "premium"
    if raw == "$2M_$250k":
        return "working"
    return "working"


def main() -> int:
    configure_logging()
    if not WHALE_LIST_PATH.exists():
        log.error("whale_list_json_missing", path=str(WHALE_LIST_PATH))
        return 2
    doc = json.loads(WHALE_LIST_PATH.read_text())
    entries = doc.get("whales", []) or []
    log.info("migrate_start", n_in_json=len(entries))

    now = datetime.now(timezone.utc)
    n_inserted = 0
    n_skipped = 0
    n_failed = 0
    with session_scope() as s:
        for entry in entries:
            addr = entry.get("address")
            if not addr:
                n_failed += 1
                continue
            existing = s.get(StructureWhalePool, addr)
            if existing is not None:
                n_skipped += 1
                continue
            try:
                s.add(StructureWhalePool(
                    address=addr,
                    added_at=now,
                    source="legacy_migration",
                    tier=_tier_from_entry(entry),
                    tag=entry.get("tag") or None,
                    historical_win_rate=entry.get("historical_win_rate"),
                    closed_positions_6mo=entry.get("closed_positions_6mo"),
                    cumulative_notional_usd=entry.get("cumulative_notional_usd"),
                    avg_hold_minutes=entry.get("avg_hold_minutes"),
                    current_max_position_usd=entry.get("current_max_position_usd"),
                    metrics_refreshed_at=now,
                ))
                n_inserted += 1
            except Exception as e:
                log.exception("insert_failed", address=addr, error=str(e))
                n_failed += 1

    log.info(
        "migrate_done", inserted=n_inserted, skipped=n_skipped, failed=n_failed
    )
    write_audit(
        "structure_whale_pool_initial_migration",
        payload={
            "json_path": str(WHALE_LIST_PATH),
            "inserted": n_inserted,
            "skipped": n_skipped,
            "failed": n_failed,
        },
    )
    return 0 if n_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
