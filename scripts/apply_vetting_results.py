"""Apply browser-Opus wallet-vetting verdicts to the COPY wallet_pool.

Browser-Opus vets unvetted watch wallets (see the vetting prompt) and writes
vetted_watch_results.csv with a verdict per wallet. This script applies them:

  KEEP   -> tier='active' + source='browser_opus_vetted' (used immediately —
            a vetted wallet should be active and analyzed, not parked cold;
            new wallets are inserted directly at active)
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

try:
    from framework.alerts import emit_alert
    from monitoring.alerting.taxonomy import Severity
    _ALERTS = True
except Exception:
    _ALERTS = False


# .txt extension (CSV content) — OneDrive blocks browser-Opus writing .csv,
# same as curated_wallets.txt. The parser is extension-agnostic.
DEFAULT_FILE = Path(__file__).resolve().parent.parent / "bots" / "copy" / "vetted_watch_results.txt"
_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
VETTED_SOURCE = "browser_opus_vetted"


def _sync_helius_from_db() -> None:
    """Sync the Helius active+watch webhooks to the current DB pool tiers.

    Called right after a vetting apply so newly-active wallets start receiving
    webhook deliveries immediately (and pruned wallets unsubscribe) — no waiting
    for the daily wallet_pool cron. Reuses the daily cron's _sync_helius. Lazy
    imports keep this script's startup light and avoid import cycles.
    """
    import asyncio
    import os
    try:
        from bots.copy.config import get_copy_settings
        from scripts.wallet_pool_daily_cron import _sync_helius
    except Exception as e:  # pragma: no cover - defensive
        print(f"\n[helius] sync unavailable ({e}) — subscriptions update on next pool cron",
              file=sys.stderr)
        return
    settings = get_copy_settings()
    api_key = settings.helius_api_key
    auth = os.environ.get("HELIUS_WEBHOOK_AUTH_SECRET", "")
    active_url = os.environ.get("COPY_WEBHOOK_URL", "")
    watch_url = os.environ.get("COPY_WEBHOOK_URL_WATCH", "")
    if not (api_key and auth and active_url and watch_url):
        print("\n[helius] sync skipped — missing env "
              "(HELIUS_WEBHOOK_AUTH_SECRET / COPY_WEBHOOK_URL[_WATCH])")
        return
    try:
        res = asyncio.run(_sync_helius(api_key, auth, active_url, watch_url))
        print(f"\n[helius] synced: active={res['active'][1]}, watch={res['watch'][1]}")
    except Exception as e:
        print(f"\n[helius] sync FAILED: {e} — retry via the daily pool cron", file=sys.stderr)


def _apply(file_path: Path, *, dry_run: bool, sync_helius: bool = True) -> int:
    if not file_path.exists():
        print(f"ERROR: file not found: {file_path}", file=sys.stderr)
        return 2

    rows = list(csv.reader(file_path.read_text(encoding="utf-8-sig").splitlines()))
    kept = rejected = skipped = notfound = bad = inserted_new = too_fast = 0
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
                # Not in the pool. A KEEP here is a NEW vetted wallet (the common
                # case for browser-discovery output). Insert it DIRECTLY at
                # active/vetted — a vetted wallet should be used and analyzed, not
                # parked cold (Roy 2026-06-24). A REJECT on a wallet not in the
                # pool is a no-op.
                if verdict == "KEEP":
                    if not dry_run:
                        s.add(WalletPool(address=addr, chain="solana",
                                         tier="active", source=VETTED_SOURCE,
                                         added_at=now, promoted_at=now))
                    print(f"  KEEP+NEW {addr[:14]}…  inserted -> active/{VETTED_SOURCE}")
                    kept += 1
                    inserted_new += 1
                else:
                    notfound += 1
                continue

            if verdict == "KEEP":
                if w.tier == "pruned":
                    # Don't auto-resurrect a wallet pruned for cause (e.g. a COPY
                    # underperformer). Leaving pruned; revisit re-vetting policy.
                    print(f"  [skip] {addr[:14]}… KEEP but already pruned — leaving pruned")
                    skipped += 1
                    continue
                if w.tier == "watch" and w.demoted_at is not None:
                    # Demoted-for-cause by the daily pool cron (proven money-loser,
                    # or quiet). That attribution/activity-based judgment is REAL
                    # performance and OUTRANKS this a-priori KEEP. Re-promoting here
                    # is the resurrection loop: the cron demotes a loser active→watch
                    # at 07:00, then this apply re-promoted it watch→active at 07:30,
                    # so demotion never stuck for KEEP wallets. Leave it on watch —
                    # the daily cron owns watch<->active by merit (it drops
                    # watch-losers to pruned and re-promotes recovered ones). Only a
                    # never-demoted watch wallet (demoted_at is None) is promoted by a
                    # KEEP. (2026-06-26 — fixes the loop the dropped exclude-worst
                    # rule exposed.)
                    print(f"  [skip] {addr[:14]}… KEEP but demoted-for-cause (watch) — leaving for the pool cron")
                    skipped += 1
                    continue
                if not dry_run:
                    w.source = VETTED_SOURCE
                    if w.tier != "active":
                        w.tier = "active"
                        w.promoted_at = now
                print(f"  KEEP   {addr[:14]}…  tier -> active / {VETTED_SOURCE}")
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
            elif verdict in ("TOO_FAST", "TOOFAST"):
                # Good wallet but sub-15-min holds — unfollowable by COPY's
                # latency. NOT ingested into COPY (logged-only for a future
                # fast/MEV-grade bot). Don't touch its pool state.
                too_fast += 1
            else:
                print(f"  [skip] {addr[:14]}… unknown verdict '{verdict}'")
                skipped += 1

    action = "would apply" if dry_run else "applied"
    print(f"\n{action}: {kept} KEEP (->vetted, {inserted_new} new inserted), "
          f"{rejected} REJECT (->pruned)")
    print(f"  too_fast (logged, not ingested): {too_fast}  skipped: {skipped}  "
          f"reject-not-in-pool(noop): {notfound}  malformed: {bad}")
    if not dry_run and (kept or rejected):
        write_audit("wallet_vetting_applied", bot_id="copy", actor="roy",
                    payload={"kept": kept, "rejected": rejected})
        # Push the updated active/watch sets to Helius NOW so newly-active wallets
        # receive webhook deliveries immediately and pruned wallets unsubscribe —
        # no waiting for the daily wallet_pool cron. (Replaces the old "happens on
        # the next daily sync" behavior, which was left over from the discovery crons.)
        if sync_helius:
            _sync_helius_from_db()
        else:
            print("\n[helius] sync skipped (--no-sync-helius) — subscriptions "
                  "update on the next wallet_pool cron")
        # Notify so an unattended cron run is visible (P2, no ping).
        if _ALERTS:
            try:
                emit_alert(
                    severity=Severity.P2,
                    title="[copy] wallet vetting applied",
                    body=(f"KEEP -> vetted: {kept}\nREJECT -> pruned: {rejected}\n"
                          f"skipped: {skipped}  not-in-pool: {notfound}  malformed: {bad}\n"
                          f"Source file: {file_path.name}"),
                    bot_id="copy", event_type="wallet_vetting_applied",
                    metadata={"kept": kept, "rejected": rejected},
                )
            except Exception:
                pass
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Apply browser-Opus vetting verdicts")
    p.add_argument("--file", default=str(DEFAULT_FILE))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-sync-helius", action="store_true",
                   help="Don't push pool changes to Helius after applying "
                        "(subscriptions then update on the next wallet_pool cron)")
    args = p.parse_args()
    return _apply(Path(args.file), dry_run=args.dry_run, sync_helius=not args.no_sync_helius)


if __name__ == "__main__":
    sys.exit(main())
