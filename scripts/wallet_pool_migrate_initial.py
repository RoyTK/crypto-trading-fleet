"""One-shot Phase A migration: populate wallet_pool table from wallet_pool.json.

All 119 (or however many) Solana wallets currently in bots/copy/wallet_pool.json
get inserted as tier='active' so the live bot keeps working identically
during the 14-day observation phase. After observation, the daily cron's
formal partition (top ~75 by 14d event count → keep active, rest →
demote to watch) takes over.

Idempotent: skips addresses already in wallet_pool.

Run once on Hetzner after alembic 0003 upgrade:
    docker compose exec -T framework python -m scripts.wallet_pool_migrate_initial
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from framework.audit import write_audit
from framework.db import session_scope
from framework.logging_setup import configure_logging, get_logger
from framework.models import WalletPool


log = get_logger("wallet_pool_migrate_initial")

WALLET_POOL_PATH = Path("bots/copy/wallet_pool.json")


def main() -> int:
    configure_logging()
    if not WALLET_POOL_PATH.exists():
        log.error("wallet_pool_json_missing", path=str(WALLET_POOL_PATH))
        return 2
    doc = json.loads(WALLET_POOL_PATH.read_text())
    entries = doc.get("wallets", []) or []
    log.info("migrate_start", n_in_json=len(entries))

    now = datetime.now(timezone.utc)
    n_inserted = 0
    n_skipped = 0
    with session_scope() as s:
        for entry in entries:
            addr = entry.get("address")
            chain = entry.get("chain") or "solana"
            if not addr or chain != "solana":
                continue
            existing = s.get(WalletPool, addr)
            if existing is not None:
                n_skipped += 1
                continue
            s.add(WalletPool(
                address=addr,
                chain=chain,
                tier="active",  # all start as active during observation
                added_at=now,
                source="legacy_migration",
            ))
            n_inserted += 1

    log.info("migrate_done", inserted=n_inserted, skipped=n_skipped)
    write_audit(
        "wallet_pool_initial_migration",
        payload={
            "json_path": str(WALLET_POOL_PATH),
            "inserted": n_inserted,
            "skipped": n_skipped,
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
