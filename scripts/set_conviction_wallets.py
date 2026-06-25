"""Manage the COPY conviction roster (wallet_pool.conviction).

The conviction (single-wallet trigger) strategy fires on a buy from any wallet
flagged `conviction = true`. The roster lives in the DB so it can be changed
WITHOUT a redeploy — just run this script (the bot reloads the roster on its
next restart; force a reload with `docker compose restart copy` if you want it
picked up immediately).

IMPORTANT: a conviction wallet only produces triggers if it is ALSO active
tier — only active-tier webhook events reach the copy:buys channel the bot
consumes. This script warns if you flag a non-active wallet.

Usage (inside the framework container, or anywhere with DB access):
  # show the current roster
  python -m scripts.set_conviction_wallets --list

  # add wallets (space-separated) with a reason
  python -m scripts.set_conviction_wallets --add ADDR1 ADDR2 --reason "elite seed 2026-06-24"

  # add from a file (one address per line; blank lines / # comments ignored)
  python -m scripts.set_conviction_wallets --add-file roster.txt --reason "seed"

  # remove wallets from the roster
  python -m scripts.set_conviction_wallets --remove ADDR1

  # also promote flagged wallets to active tier (so their buys actually arrive)
  python -m scripts.set_conviction_wallets --add ADDR1 --reason seed --ensure-active
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import text

from framework.db import session_scope


def _now():
    return datetime.now(timezone.utc)


def _read_file(path: str) -> list[str]:
    out: list[str] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # tolerate "addr,..." rows by taking the first comma-field
            out.append(line.split(",")[0].strip())
    return out


def _print_roster() -> None:
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT address, tier, conviction_reason, conviction_at, "
            "       cielo_pnl_90d_usd, pinned "
            "FROM wallet_pool WHERE conviction = true "
            "ORDER BY tier, address"
        )).all()
    if not rows:
        print("conviction roster is EMPTY")
        return
    print(f"conviction roster ({len(rows)} wallets):")
    n_active = 0
    for r in rows:
        if r.tier == "active":
            n_active += 1
        flag = "" if r.tier == "active" else f"  ⚠ tier={r.tier} (won't trigger)"
        pnl = f"${r.cielo_pnl_90d_usd:,.0f}" if r.cielo_pnl_90d_usd is not None else "—"
        pin = " [pinned]" if r.pinned else ""
        print(f"  {r.address}  {pnl:>12}{pin}{flag}  {r.conviction_reason or ''}")
    print(f"\n{n_active}/{len(rows)} are active-tier (only active-tier wallets produce triggers).")


def _apply(addresses: list[str], *, add: bool, reason: str, ensure_active: bool) -> int:
    addresses = [a for a in dict.fromkeys(addresses) if a]  # dedupe, keep order
    if not addresses:
        print("no addresses given", file=sys.stderr)
        return 2
    changed = 0
    missing: list[str] = []
    non_active: list[str] = []
    with session_scope() as s:
        for addr in addresses:
            row = s.execute(text(
                "SELECT address, tier FROM wallet_pool WHERE address = :a"
            ), {"a": addr}).first()
            if row is None:
                missing.append(addr)
                continue
            if add:
                s.execute(text(
                    "UPDATE wallet_pool SET conviction = true, "
                    "conviction_at = :now, conviction_reason = :reason "
                    "WHERE address = :a"
                ), {"now": _now(), "reason": reason, "a": addr})
                if row.tier != "active":
                    non_active.append(addr)
                    if ensure_active:
                        s.execute(text(
                            "UPDATE wallet_pool SET tier = 'active', "
                            "promoted_at = :now WHERE address = :a"
                        ), {"now": _now(), "a": addr})
            else:
                s.execute(text(
                    "UPDATE wallet_pool SET conviction = false WHERE address = :a"
                ), {"a": addr})
            changed += 1
        # COMMIT is implicit on session_scope exit, but be explicit per repo
        # convention for raw-SQL writes.
        s.execute(text("COMMIT"))

    verb = "flagged" if add else "removed"
    print(f"{verb} {changed} wallet(s).")
    if missing:
        print(f"⚠ NOT in wallet_pool (skipped): {', '.join(missing)}", file=sys.stderr)
    if add and non_active:
        if ensure_active:
            print(f"promoted to active so triggers arrive: {', '.join(non_active)}")
        else:
            print("⚠ flagged but NOT active-tier (won't trigger until active; "
                  f"re-run with --ensure-active): {', '.join(non_active)}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Manage the COPY conviction roster.")
    ap.add_argument("--list", action="store_true", help="print the current roster and exit")
    ap.add_argument("--add", nargs="*", default=[], metavar="ADDR", help="addresses to flag")
    ap.add_argument("--add-file", metavar="PATH", help="file of addresses to flag (one per line)")
    ap.add_argument("--remove", nargs="*", default=[], metavar="ADDR", help="addresses to unflag")
    ap.add_argument("--reason", default="", help="conviction_reason note (for --add)")
    ap.add_argument("--ensure-active", action="store_true",
                    help="also set tier='active' for added wallets (so their buys arrive)")
    args = ap.parse_args()

    if args.list:
        _print_roster()
        return 0

    add_list = list(args.add)
    if args.add_file:
        add_list += _read_file(args.add_file)

    if add_list and args.remove:
        print("provide either --add/--add-file OR --remove, not both", file=sys.stderr)
        return 2

    if add_list:
        if not args.reason:
            print("--reason is required when adding wallets", file=sys.stderr)
            return 2
        rc = _apply(add_list, add=True, reason=args.reason, ensure_active=args.ensure_active)
    elif args.remove:
        rc = _apply(list(args.remove), add=False, reason="", ensure_active=False)
    else:
        ap.print_help()
        return 0

    print()
    _print_roster()
    return rc


if __name__ == "__main__":
    sys.exit(main())
