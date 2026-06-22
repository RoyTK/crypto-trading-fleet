"""Apply browser-Opus wallet-vetting verdicts to the COPY wallet_pool.

Browser-Opus vets unvetted watch wallets (see the vetting prompt) and writes
vetted_watch_results.csv with a verdict per wallet. This script applies them:

  KEEP   -> source='browser_opus_vetted'  (promotion priority 0, ahead of
            raw birdeye_gainers; tier unchanged — the daily cron promotes it)
  REJECT -> tier='pruned' + demoted_at=now (unsubscribed from Helius next
            cron sync; row kept for re-discovery dedup)

The vetting found ~90% of auto-discovered wallets are noise (bag-holders,
MM bots, dust-churners, airdrop-dump wallets), so REJECT is the common case.

Idempotent: re-applying the same verdicts is a no-op. --dry-run previews.

CSV columns (from the vetting prompt): address, verdict, realized_pnl,
unrealized_pnl, avg_hold, rug_pct, multi_x_wins, reason, date_vetted.
Only address + verdict are used.

Usage (inside the framework container):
  docker compose exec -T framework python -m scripts.apply_vetting_results --dry-run
  docker compose exec -T framework python -m scripts.apply_vetting_results
  docker compose exec -T framework python -m scripts.apply_vetting_results --file /app/bots/copy/vetted_watch_results.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from framework.db import session_scope
from framework.audit import write_audit
from framework.models import WalletPool


DEFAULT_FILE = Path(__file__).resolve().parent.parent / "bots" / "copy" / "vetted_watch_results.csv"
_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
VETTED_SOURCE = "browser_opus_vetted"


def _apply(file_path: Path, *, dry_run: bool) -> int:
    if not file_path.exists():
        print(f"ERROR: file not found: {file_path}", file=sys.stderr)
        return 2

    rows = list(csv.reader(file_path.read_text(encoding="utf-8-sig").splitlines()))
    kept = rejected = skipped = notfound = bad = 0
    now = datetime.now(timezone.utc)

    with session_scope() as s:
        for r in rows:
            if not r or not r[0].strip():
                continue
            addr = r[0].strip().strip('"')
            if addr.lower().startswith("wallet") or addr.lower() == "address":
                continue  # header
            if not _B58.match(addr):
                bad += 1
                continue
            verdict = (r[1].strip().upper() if len(r) > 1 else "")
            w = s.get(WalletPool, addr)
            if w is None:
                print(f"  [not-found] {addr[:14]}…")
                notfound += 1
                continue

            if verdict == "KEEP":
                if w.tier == "pruned":
                    print(f"  [skip] {addr[:14]}… KEEP but already pruned — leaving pruned")
                    skipped += 1
                    continue
                if not dry_run:
                    w.source = VETTED_SOURCE
                print(f"  KEEP   {addr[:14]}…  source -> {VETTED_SOURCE}")
                kept += 1
            elif verdict == "REJECT":
                if w.tier == "pruned":
                    skipped += 1
                    continue
                if not dry_run:
                    w.tier = "pruned"
                    w.demoted_at = now
                print(f"  REJECT {addr[:14]}…  tier {w.tier} -> pruned")
                rejected += 1
            else:
                print(f"  [skip] {addr[:14]}… unknown verdict '{verdict}'")
                skipped += 1

    action = "would apply" if dry_run else "applied"
    print(f"\n{action}: {kept} KEEP (->vetted), {rejected} REJECT (->pruned)")
    print(f"  skipped: {skipped}  not-in-pool: {notfound}  malformed: {bad}")
    if not dry_run and (kept or rejected):
        write_audit("wallet_vetting_applied", bot_id="copy", actor="roy",
                    payload={"kept": kept, "rejected": rejected})
        print("\nPruned wallets unsubscribe from Helius on the next daily cron "
              "sync. Kept wallets promote vetted-first (cap 10/day).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Apply browser-Opus vetting verdicts")
    p.add_argument("--file", default=str(DEFAULT_FILE))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    return _apply(Path(args.file), dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
