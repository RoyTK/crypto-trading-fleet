"""Ingest browser-Opus-curated Solana trader wallets into the COPY watch tier.

Browser-Opus discovers early-token trader wallets on Birdeye (Trending ->
Top Traders -> Smart Money) and appends them to a CSV-content .txt file
(`SOL_early_token_traders.txt`) on OneDrive. Roy commits that file into the
repo; this script parses it and upserts NEW wallets into the wallet_pool DB
table at tier='watch'.

Everything lands at WATCH, never active — deliberately. Leaderboard /
Smart-Money-tagged wallets proved ~80% funds/treasuries/MM-bots in the
2026-06-06 deep-dive, so the existing watch->active promotion machinery
(wallet_pool_daily_cron + decide_tier_changes) is the real validation gate:
it observes each wallet's actual cluster behavior before any reach live
trading. The daily cron also auto-subscribes new watch wallets to the
Helius WATCH webhook (its _sync_helius reads the DB), so there is no
JSON/webhook step to do here.

Idempotent: re-reads the file each run and inserts only addresses not
already in the pool (any tier). Safe to run daily on a cron — new rows from
the latest curation get picked up within a day, dupes are no-ops.

Screened OUT (not ingested):
  - the header row, blanks, malformed (non-base58) addresses
  - rows marked EXCLUDED or with negative lifetime PnL
  - Sniper=Yes (HFT/sniper screen — we've had to demote MM-bots that
    polluted cluster signals: CreQJ2t9, FrhLfj81)

Per-wallet curation metadata (PnL, age, multi-x, cluster, source tokens,
notes, curation date) is written to audit_log
(event_type='wallet_curation_ingest') for the durable record + later
analysis, since wallet_pool has no freeform column. The cluster tag is also
folded into the wallet_pool.source field (e.g. 'browser_opus:HUNTER-cluster-A')
for at-a-glance visibility in the dashboard.

CSV columns (exact order, .txt extension but CSV content):
  Wallet Address (FULL), Lifetime Total PnL, Wallet Age, # Multi-x Wins,
  Smart Money?, Sniper?, Builder?, Source Token(s), Cluster, Notes,
  [Date Curated]   <- 11th col optional; older rows don't have it

Usage (inside the framework container against the prod DB):
  docker compose exec -T framework python -m scripts.ingest_curated_wallets
  docker compose exec -T framework python -m scripts.ingest_curated_wallets --dry-run
  docker compose exec -T framework python -m scripts.ingest_curated_wallets --file /app/bots/copy/curated_wallets.txt
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import select

from framework.db import session_scope
from framework.audit import write_audit
from framework.models import WalletPool


# Default committed location. Roy copies the OneDrive .txt here and commits;
# the Hetzner cron does `git pull` then runs this script.
DEFAULT_FILE = Path(__file__).resolve().parent.parent / "bots" / "copy" / "curated_wallets.txt"

# Solana base58 address: 32-44 chars, no 0/O/I/l.
_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

INGEST_EVENT = "wallet_curation_ingest"
SOURCE_PREFIX = "browser_opus"


def _parse_pnl(raw: str) -> Optional[float]:
    """'+$54.62K' -> 54620.0, '-$4.92K' -> -4920.0, '+$792.97*' -> 792.97.
    Returns None on '-' / blank / unparseable."""
    s = (raw or "").strip().replace("$", "").replace(",", "").replace("*", "").replace("+", "")
    if not s or s == "-":
        return None
    neg = s.startswith("-")
    s = s.lstrip("-").strip()
    mult = 1.0
    if s[-1:].upper() == "K":
        mult, s = 1e3, s[:-1]
    elif s[-1:].upper() == "M":
        mult, s = 1e6, s[:-1]
    try:
        v = float(s) * mult
    except ValueError:
        return None
    return -v if neg else v


def _yes(v: str) -> bool:
    return (v or "").strip().lower() in ("yes", "y", "true", "1")


def _ingest(file_path: Path, *, dry_run: bool) -> int:
    if not file_path.exists():
        print(f"ERROR: file not found: {file_path}", file=sys.stderr)
        return 2

    rows = list(csv.reader(file_path.read_text(encoding="utf-8-sig").splitlines()))
    if not rows:
        print("(empty file)", file=sys.stderr)
        return 0

    with session_scope() as s:
        existing = {a for (a,) in s.execute(select(WalletPool.address)).all()}

    inserted = excluded = sniper_skip = malformed = dupe = 0
    now = datetime.now(timezone.utc)
    to_insert: list[tuple[str, str, dict]] = []  # (address, source, meta)

    for r in rows:
        if not r or not r[0].strip():
            continue
        addr = r[0].strip().strip('"')
        # header
        if addr.lower().startswith("wallet address"):
            continue
        # pad to 11 cols
        r = (r + [""] * 11)[:11]
        pnl_raw, age, multi_x = r[1].strip(), r[2].strip(), r[3].strip()
        smart, sniper, builder = r[4].strip(), r[5].strip(), r[6].strip()
        src_tokens, cluster, notes, curated = r[7].strip(), r[8].strip(), r[9].strip(), r[10].strip()

        if not _B58.match(addr):
            malformed += 1
            continue
        if "excluded" in notes.lower():
            excluded += 1
            continue
        pnl = _parse_pnl(pnl_raw)
        if pnl is not None and pnl < 0:
            excluded += 1
            continue
        if _yes(sniper):
            sniper_skip += 1
            continue
        if addr in existing:
            dupe += 1
            continue

        existing.add(addr)  # guard against in-file dupes
        source = f"{SOURCE_PREFIX}:{cluster}"[:64] if cluster else SOURCE_PREFIX
        meta = {
            "address": addr,
            "lifetime_pnl_raw": pnl_raw,
            "lifetime_pnl_usd": pnl,
            "wallet_age": age,
            "multi_x_wins": multi_x,
            "smart_money": _yes(smart),
            "sniper": _yes(sniper),
            "builder": _yes(builder),
            "source_tokens": src_tokens,
            "cluster": cluster or None,
            "notes": notes or None,
            "curated_date": curated or None,
        }
        to_insert.append((addr, source, meta))
        inserted += 1

    if dry_run:
        print(f"\n[DRY RUN] would insert {inserted} new watch wallets:")
        for addr, source, meta in to_insert:
            cl = f"  cluster={meta['cluster']}" if meta["cluster"] else ""
            print(f"  {addr}  pnl={meta['lifetime_pnl_raw']}  age={meta['wallet_age']}  src={source}{cl}")
    else:
        with session_scope() as s:
            for addr, source, _meta in to_insert:
                s.add(WalletPool(
                    address=addr,
                    chain="solana",
                    tier="watch",
                    source=source,
                    added_at=now,
                ))
        # audit rows (separate txn so a row-level failure doesn't roll back the inserts)
        for _addr, _source, meta in to_insert:
            try:
                write_audit(INGEST_EVENT, bot_id="copy", actor="roy", payload=meta)
            except Exception:
                pass

    action = "would add" if dry_run else "added"
    print(f"\ningest complete: {action} {inserted} new watch wallets")
    print(f"  skipped — already in pool: {dupe}")
    print(f"  skipped — EXCLUDED/negative PnL: {excluded}")
    print(f"  skipped — Sniper=Yes (HFT screen): {sniper_skip}")
    print(f"  skipped — malformed address: {malformed}")
    if not dry_run and inserted:
        print("\nNew wallets are at tier='watch'. The daily wallet_pool cron will "
              "subscribe them to the Helius watch webhook and begin the "
              "watch->active validation. None will trade until promoted.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Ingest curated Solana wallets into COPY watch tier")
    p.add_argument("--file", default=str(DEFAULT_FILE),
                   help=f"Path to the curated CSV/.txt (default: {DEFAULT_FILE})")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse + report what would be inserted, write nothing")
    args = p.parse_args()
    return _ingest(Path(args.file), dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
