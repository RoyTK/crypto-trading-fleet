"""Correct specific paper trades that were sold AFTER a rug at a fictitious
stale price (e.g. turtle, trade 783: +$295 booked on a token that had
already rugged). Recomputes the trade as a rug loss and fixes its
wallet_attributions.

DELIBERATELY targeted, not a sweep. We CANNOT auto-detect fictitious rug
exits from current data: a now-rugged token booked at a profit might be a
REAL pre-rug exit (NUT/TRILL = COPY's biggest wins, rugged AFTER we cleanly
exited) or a FICTITIOUS post-rug exit (turtle). Only a human/browser-Opus
review can tell. This script applies the correction to trade IDs you've
confirmed.

Correction model: the unsold remainder is worthless (can't sell into a
rug). Any partials already banked (sold while liquidity existed) are KEPT.
  corrected_pnl_usd = (sum of filled partial received_usdc) - size_usd
  corrected_pnl_pct = corrected_pnl_usd / size_usd * 100
For a no-partial trade that's ~ -size_usd (total loss). Per-wallet
attributions are rescaled to the corrected PnL (equal-share / cluster_size).

Note (Roy): one rug does NOT make a wallet bad — this only corrects the
fictitious PnL of the specific trade; the wallet's net record across all
its trades is what the demotion/prune logic judges.

Usage:
  docker compose exec -T framework python -m scripts.correct_rug_trade 783 --dry-run
  docker compose exec -T framework python -m scripts.correct_rug_trade 783
  docker compose exec -T framework python -m scripts.correct_rug_trade 783 901 902
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from framework.db import session_scope
from framework.audit import write_audit
from framework.models import Trade, WalletAttribution


def _banked_from_partials(sim_metadata: dict) -> float:
    partials = (sim_metadata or {}).get("partial_exits") or []
    return sum(
        float(p.get("received_usdc", 0.0)) for p in partials
        if isinstance(p, dict) and p.get("status") == "filled"
    )


def _correct(trade_ids: list[int], *, dry_run: bool, no_attribution: bool) -> int:
    for tid in trade_ids:
        with session_scope() as s:
            t = s.get(Trade, tid)
            if t is None:
                print(f"[{tid}] not found", file=sys.stderr)
                continue
            if t.fill_status != "closed":
                print(f"[{tid}] not closed (fill_status={t.fill_status}) — skip", file=sys.stderr)
                continue
            size_usd = float(t.size_usd or 0.0)
            entry = float(t.entry_price or 0.0)
            md = dict(t.sim_metadata or {})
            if md.get("rug_corrected"):
                print(f"[{tid}] already corrected — skip")
                continue

            banked = _banked_from_partials(md)
            corrected_pnl = banked - size_usd
            corrected_pct = (corrected_pnl / size_usd * 100.0) if size_usd else -100.0

            print(f"\n[{tid}] {t.asset}")
            print(f"  BEFORE: pnl_usd={float(t.pnl_usd or 0):.2f}  pnl_pct={float(t.pnl_pct or 0):.1f}  "
                  f"exit_price={t.exit_price}  exit_reason={t.exit_reason}")
            print(f"  banked partials: ${banked:.2f}  size: ${size_usd:.2f}")
            print(f"  AFTER : pnl_usd={corrected_pnl:.2f}  pnl_pct={corrected_pct:.1f}  "
                  f"exit_reason=rug_corrected")

            # Show attribution impact
            attrs = s.execute(text(
                "SELECT id, wallet_address, cluster_size, attributed_pnl_usd "
                "FROM wallet_attributions WHERE trade_id=:tid"), {"tid": tid}).all()
            for a in attrs:
                cs = int(a.cluster_size) or 1
                new_share = corrected_pnl / cs
                print(f"    attr {a.wallet_address[:12]}… {float(a.attributed_pnl_usd):.2f} -> {new_share:.2f}")

            if dry_run:
                continue

            # Apply: trade
            md["rug_corrected"] = True
            md["orig_pnl_usd"] = float(t.pnl_usd or 0.0)
            md["orig_exit_price"] = t.exit_price
            t.exit_price = entry * 1e-6 if entry else 0.0
            t.pnl_usd = corrected_pnl
            t.pnl_pct = corrected_pct
            t.exit_reason = "rug_corrected"
            t.sim_metadata = md
            # Apply: attributions (rescale to corrected pnl, equal-share).
            # SKIP when --no-attribution: use for losses caused by COPY's OWN
            # execution bug where the tracked wallets actually exited fine
            # (sold before the rug) — debiting them would wrongly condemn a
            # good signal. (Not turtle: those wallets held into the rug.)
            if no_attribution:
                print("    (--no-attribution: wallet attributions left unchanged)")
            else:
                for a in attrs:
                    cs = int(a.cluster_size) or 1
                    s.execute(text(
                        "UPDATE wallet_attributions SET attributed_pnl_usd=:p, "
                        "attributed_pnl_pct=:pct WHERE id=:id"),
                        {"p": corrected_pnl / cs, "pct": corrected_pct, "id": a.id})

        if not dry_run:
            write_audit("rug_trade_corrected", bot_id="copy", actor="roy",
                        payload={"trade_id": tid, "corrected_pnl_usd": corrected_pnl})
            print(f"[{tid}] corrected.")

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Correct fictitious post-rug paper exits")
    p.add_argument("trade_ids", nargs="+", type=int, help="Reviewed trade IDs to correct")
    p.add_argument("--dry-run", action="store_true", help="Show changes, write nothing")
    p.add_argument("--no-attribution", action="store_true",
                   help="Correct trade PnL but leave wallet attributions "
                        "unchanged (loss was COPY's execution fault, wallets "
                        "exited fine)")
    args = p.parse_args()
    return _correct(args.trade_ids, dry_run=args.dry_run, no_attribution=args.no_attribution)


if __name__ == "__main__":
    sys.exit(main())
