"""Curate the COPY bot's wallet pool by validating hand-seeded candidates.

Cielo doesn't expose a top-traders / leaderboard REST endpoint — wallet
discovery has to come from external sources (Birdeye smart-money, DeBank
profiles, X/Twitter accounts, Hyperliquid leaderboard scrape). This script
takes a hand-curated seed list and validates each via Cielo's
/trading-stats endpoint, applying the Item #7 filters.

Build A is Solana-only. EVM (Base + Arbitrum) joins in Build B once we
either upgrade Cielo or switch to webhook delivery (polling 90 EVM wallets
through Cielo Pro's 50k credit/mo budget is infeasible).

Usage:
    # Edit bots/copy/wallet_seeds.json with your candidate addresses, then:
    python -m scripts.curate_wallet_pool --dry-run
    python -m scripts.curate_wallet_pool --apply

Each wallet validated costs 30 Cielo credits. 50 hand-seed candidates =
1500 credits, ~3% of monthly budget. Safe to re-run.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from bots.copy.venue.cielo import CieloClient, WalletStats, passes_curation_filters
from framework.audit import write_audit
from framework.logging_setup import configure_logging, get_logger


REPO_ROOT = Path(__file__).resolve().parent.parent
WALLET_POOL_PATH = REPO_ROOT / "bots" / "copy" / "wallet_pool.json"
WALLET_SEEDS_PATH = REPO_ROOT / "bots" / "copy" / "wallet_seeds.json"

# Build A: Solana-only. Set to {"solana", "base", "arbitrum"} when EVM lands.
BUILD_A_CHAINS = {"solana"}


async def curate(dry_run: bool, log) -> int:
    cielo = CieloClient()
    if not cielo.settings.cielo_api_key:
        print("ERROR: CIELO_API_KEY not configured. Aborting.", file=sys.stderr)
        return 2

    seeds = _load_seeds()
    if not seeds:
        print(f"ERROR: no seed wallets found in {WALLET_SEEDS_PATH}.", file=sys.stderr)
        print("       Edit that file with hand-curated candidate addresses, then re-run.", file=sys.stderr)
        return 2

    in_scope = [s for s in seeds if s.get("chain") in BUILD_A_CHAINS]
    out_of_scope = [s for s in seeds if s.get("chain") not in BUILD_A_CHAINS]
    print(f"Loaded {len(seeds)} seed wallets ({len(in_scope)} in-scope for Build A, {len(out_of_scope)} skipped)")
    if out_of_scope:
        skipped_chains = sorted({s.get("chain") for s in out_of_scope})
        print(f"  skipped chains (Build B): {skipped_chains}")

    passed: list[dict] = []
    rejected: list[dict] = []
    errors: list[dict] = []

    async with aiohttp.ClientSession() as session:
        for i, seed in enumerate(in_scope, 1):
            address = seed.get("address")
            chain = seed.get("chain")
            note = seed.get("note", "")
            print(f"[{i}/{len(in_scope)}] {chain} {address[:8]}...{address[-6:]} ", end="", flush=True)

            stats = await cielo.wallet_trading_stats(session, address, chain, days="max")
            if stats is None:
                print("ERROR (no stats returned)")
                errors.append({"address": address, "chain": chain, "note": note})
                continue

            ok, reasons = passes_curation_filters(stats)
            entry = _to_entry(stats, note)
            if ok:
                passed.append(entry)
                print(f"OK pnl=${stats.pnl_usd:.0f} wr={stats.win_rate:.2f} "
                      f"swaps={stats.swap_count} streak={stats.consecutive_trading_days}d")
            else:
                rejected.append({"address": address, "chain": chain, "note": note,
                                 "stats": entry, "reasons": reasons})
                print(f"REJECT reasons={reasons}")

    # Sort qualifying wallets by pnl × winrate
    passed.sort(key=lambda e: e["pnl_usd"] * e["win_rate"], reverse=True)

    print()
    print("=" * 60)
    print(f"Validated:  {len(in_scope)}")
    print(f"Passed:     {len(passed)}")
    print(f"Rejected:   {len(rejected)}")
    print(f"Errors:     {len(errors)}")
    print()
    if rejected:
        from collections import Counter
        reason_counts = Counter()
        for r in rejected:
            for reason in r["reasons"]:
                reason_counts[reason] += 1
        print("Rejection reasons:")
        for reason, n in reason_counts.most_common():
            print(f"  {reason}: {n}")
    print()
    if passed:
        print("Top 10 by pnl × winrate:")
        for e in passed[:10]:
            print(f"  {e['chain']:8s} {e['address'][:8]}...{e['address'][-6:]}"
                  f"  pnl=${e['pnl_usd']:>8.0f}  wr={e['win_rate']:.2f}  swaps={e['swap_count']}")

    write_audit(
        "copy_wallet_pool_curation",
        bot_id="copy",
        payload={
            "validated_count": len(in_scope),
            "passed_count": len(passed),
            "rejected_count": len(rejected),
            "error_count": len(errors),
            "dry_run": dry_run,
            "build": "A_solana_only",
        },
    )

    if dry_run:
        print()
        print("--dry-run: not writing wallet_pool.json. Re-run with --apply to commit.")
        return 0

    pool_doc = {
        "metadata": {
            "version": 1,
            "curated_at": datetime.now(timezone.utc).isoformat(),
            "source": "scripts/curate_wallet_pool.py (hand-seed validator)",
            "build": "A_solana_only",
            "criteria": {
                "min_pnl_usd": 50000,
                "min_win_rate": 0.55,
                "hold_time_minutes": [30, 10080],
                "min_swap_count": 20,
                "min_consecutive_trading_days": 30,
            },
            "stats": {
                "validated": len(in_scope),
                "passed": len(passed),
                "rejected": len(rejected),
                "errors": len(errors),
            },
        },
        "wallets": passed,
    }
    WALLET_POOL_PATH.write_text(json.dumps(pool_doc, indent=2) + "\n")
    print(f"Wrote {len(passed)} wallets to {WALLET_POOL_PATH}")
    return 0


def _load_seeds() -> list[dict]:
    if not WALLET_SEEDS_PATH.exists():
        return []
    try:
        doc = json.loads(WALLET_SEEDS_PATH.read_text())
    except Exception:
        return []
    return [w for w in doc.get("seeds", []) if w.get("address") and w.get("chain")]


def _to_entry(stats: WalletStats, note: str = "") -> dict:
    return {
        "address": stats.address,
        "chain": stats.chain,
        "pnl_usd": stats.pnl_usd,
        "win_rate": stats.win_rate,
        "avg_hold_minutes": stats.avg_hold_minutes,
        "swap_count": stats.swap_count,
        "consecutive_trading_days": stats.consecutive_trading_days,
        "timeframe": stats.timeframe,
        "note": note,
    }


def main() -> None:
    configure_logging()
    log = get_logger("scripts.curate_wallet_pool")
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = p.parse_args()
    rc = asyncio.run(curate(dry_run=args.dry_run, log=log))
    sys.exit(rc)


if __name__ == "__main__":
    main()
