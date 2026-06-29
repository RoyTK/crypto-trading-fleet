"""Apply the active-list co-buyer AUDIT verdicts to the COPY wallet_pool.

browser-Opus audits each active wallet for co-buyer participation (see
bots/copy/discovery_automation/cluster_active_audit_prompt.txt) and writes
cluster_active_audit_results.txt with a verdict per wallet. This script applies
the PRUNE verdicts:

  PRUNE       -> tier='pruned' + demoted_at=now + source='cobuyer_audit_pruned'
                 (unsubscribes from Helius on the next sync below -> stops billing).
                 REVERSIBLE: the row is kept; re-add later via re-vetting if it
                 turns out to be a real (off-list) group member.
  KEEP_ACTIVE -> no-op (stays active).
  UNCERTAIN   -> no-op (stays active; flagged for a human, never auto-pruned).

HARD SAFETY: a conviction-roster wallet (conviction=true) or a pinned wallet is
NEVER pruned, even if the results file contains a PRUNE row for it — the script
refuses and logs it. (The export already excludes them, so this is defense in depth.)

Idempotent: re-applying is a no-op (already-pruned wallets are skipped). --dry-run
previews. After applying it re-syncs the Helius active/watch webhooks so pruned
wallets actually stop delivering events (the whole point — cut credit spend).

Results-file columns (from the audit prompt): address, verdict, co_buy_evidence,
partners, n_ran_tokens, date_audited. Only address + verdict are used.

Usage (inside the framework container):
  docker compose exec -T framework python -m scripts.apply_active_audit --dry-run
  docker compose exec -T framework python -m scripts.apply_active_audit
  docker compose exec -T framework python -m scripts.apply_active_audit --file /app/bots/copy/cluster_active_audit_results.txt
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from framework.db import session_scope
from framework.audit import write_audit
from framework.models import WalletPool

try:
    from framework.alerts import emit_alert
    from monitoring.alerting.taxonomy import Severity
    _ALERTS = True
except Exception:
    _ALERTS = False


DEFAULT_FILE = Path(__file__).resolve().parent.parent / "bots" / "copy" / "cluster_active_audit_results.txt"
_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
PRUNE_SOURCE = "cobuyer_audit_pruned"


def _apply(file_path: Path, *, dry_run: bool, sync_helius: bool = True) -> int:
    if not file_path.exists():
        print(f"ERROR: file not found: {file_path}", file=sys.stderr)
        return 2

    rows = list(csv.reader(file_path.read_text(encoding="utf-8-sig").splitlines()))
    pruned = kept = uncertain = exempted = notfound = skipped = bad = 0
    now = datetime.now(timezone.utc)

    with session_scope() as s:
        for r in rows:
            if not r or not r[0].strip():
                continue
            addr = r[0].strip().strip('"')
            if addr.startswith("#"):
                continue  # comment / summary line
            if addr.lower() == "address" or addr.lower().startswith("address"):
                continue  # header
            verdict = (r[1].strip().upper() if len(r) > 1 else "")

            if verdict in ("KEEP_ACTIVE", "KEEP"):
                kept += 1
                continue
            if verdict == "UNCERTAIN":
                uncertain += 1
                continue
            if verdict != "PRUNE":
                print(f"  [skip] {addr[:14]}… unknown verdict '{verdict}'")
                skipped += 1
                continue

            if not _B58.match(addr):
                bad += 1
                continue
            w = s.get(WalletPool, addr)
            if w is None:
                notfound += 1
                continue

            # HARD SAFETY — never prune a conviction or pinned wallet.
            if getattr(w, "conviction", False) or getattr(w, "pinned", False):
                why = "conviction" if getattr(w, "conviction", False) else "pinned"
                print(f"  ⚠ EXEMPT {addr[:14]}… PRUNE refused — {why} wallet (kept active)")
                exempted += 1
                continue
            if w.tier == "pruned":
                skipped += 1
                continue
            if w.tier != "active":
                # Audit targets active wallets; a PRUNE on a non-active row is unexpected.
                print(f"  [skip] {addr[:14]}… PRUNE but tier={w.tier} (not active) — leaving")
                skipped += 1
                continue
            if not dry_run:
                w.tier = "pruned"
                w.demoted_at = now
                w.source = PRUNE_SOURCE
            print(f"  PRUNE  {addr[:14]}…  tier active -> pruned")
            pruned += 1

    action = "would prune" if dry_run else "pruned"
    print(f"\n{action}: {pruned}  (kept_active: {kept}  uncertain: {uncertain}  "
          f"exempted[conviction/pinned]: {exempted})")
    print(f"  not-in-pool: {notfound}  skipped: {skipped}  malformed: {bad}")

    if not dry_run and pruned:
        write_audit("cobuyer_audit_applied", bot_id="copy", actor="roy",
                    payload={"pruned": pruned, "kept_active": kept,
                             "uncertain": uncertain, "exempted": exempted})
        # Unsubscribe the pruned wallets from Helius NOW so they stop billing —
        # reuse the vetting apply's sync (active+watch webhooks <- DB tiers).
        if sync_helius:
            try:
                from scripts.apply_vetting_results import _sync_helius_from_db
                _sync_helius_from_db()
            except Exception as e:  # pragma: no cover - defensive
                print(f"\n[helius] sync unavailable ({e}) — subscriptions update on "
                      "the next wallet_pool cron", file=sys.stderr)
        else:
            print("\n[helius] sync skipped (--no-sync-helius) — subscriptions "
                  "update on the next wallet_pool cron")
        if _ALERTS:
            try:
                emit_alert(
                    severity=Severity.P2,
                    title="[copy] co-buyer audit applied",
                    body=(f"PRUNE -> pruned: {pruned}\nkept_active: {kept}  "
                          f"uncertain: {uncertain}\nexempted (conviction/pinned): {exempted}\n"
                          f"Source file: {file_path.name}"),
                    bot_id="copy", event_type="cobuyer_audit_applied",
                    metadata={"pruned": pruned},
                )
            except Exception:
                pass
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Apply active-list co-buyer audit verdicts")
    p.add_argument("--file", default=str(DEFAULT_FILE))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-sync-helius", action="store_true",
                   help="Don't push pool changes to Helius after applying")
    args = p.parse_args()
    return _apply(Path(args.file), dry_run=args.dry_run, sync_helius=not args.no_sync_helius)


if __name__ == "__main__":
    sys.exit(main())
