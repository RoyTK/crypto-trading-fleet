"""Curate the COPY bot's wallet pool from Cielo's top-trader leaderboards.

Pulls candidate wallets per chain from Cielo, fetches PnL/winrate stats for
each, applies the locked Item #7 filters, and writes the qualifying set to
bots/copy/wallet_pool.json.

Run after:
- HELIUS_API_KEY + CIELO_API_KEY are populated in .env
- Roy reviews `--dry-run` output (writes a candidate report + diff but
  doesn't overwrite wallet_pool.json) before committing the pool

Usage:
    python -m scripts.curate_wallet_pool --dry-run
    python -m scripts.curate_wallet_pool --apply
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from bots.copy.config import (
    WALLET_POOL_TARGET_MAX,
    WALLET_POOL_TARGET_MIN,
)
from bots.copy.venue.cielo import CieloClient, WalletStats, passes_curation_filters
from framework.audit import write_audit
from framework.logging_setup import configure_logging, get_logger


WALLET_POOL_PATH = Path(__file__).resolve().parent.parent / "bots" / "copy" / "wallet_pool.json"
CHAINS = ("solana", "base", "arbitrum")


async def curate(dry_run: bool, log) -> int:
    cielo = CieloClient()
    if not cielo.settings.cielo_api_key:
        print("ERROR: CIELO_API_KEY not configured. Aborting.", file=sys.stderr)
        return 2

    candidates_per_chain: dict[str, list[str]] = {}
    async with aiohttp.ClientSession() as session:
        for chain in CHAINS:
            addrs = await cielo.top_traders(session, chain, limit=300)
            candidates_per_chain[chain] = addrs
            print(f"[{chain}] leaderboard returned {len(addrs)} candidates")

        # Fetch PnL/winrate for each candidate; filter
        passed: list[dict] = []
        rejected: list[dict] = []
        for chain, addrs in candidates_per_chain.items():
            for addr in addrs:
                stats = await cielo.wallet_stats(session, addr, chain)
                if stats is None:
                    rejected.append({"address": addr, "chain": chain, "reasons": ["no_stats"]})
                    continue
                ok, reasons = passes_curation_filters(stats)
                entry = _to_entry(stats)
                if ok:
                    passed.append(entry)
                else:
                    rejected.append({"address": addr, "chain": chain, "reasons": reasons})

    # Sort by pnl × winrate; keep top-N
    passed.sort(key=lambda e: e["pnl_usd_180d"] * e["win_rate"], reverse=True)
    target = max(WALLET_POOL_TARGET_MIN, min(WALLET_POOL_TARGET_MAX, len(passed)))
    final = passed[:target]

    print()
    print("=" * 60)
    print(f"Pool candidates passed filters: {len(passed)}")
    print(f"Pool size after top-N cap ({WALLET_POOL_TARGET_MIN}-{WALLET_POOL_TARGET_MAX}): {len(final)}")
    print("Per-chain breakdown:")
    for chain in CHAINS:
        n = sum(1 for e in final if e["chain"] == chain)
        print(f"  {chain}: {n}")

    write_audit(
        "copy_wallet_pool_curation",
        bot_id="copy",
        payload={
            "passed_count": len(passed),
            "final_count": len(final),
            "rejected_count": len(rejected),
            "dry_run": dry_run,
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
            "source": "scripts/curate_wallet_pool.py",
            "criteria": {
                "min_pnl_usd_180d": 50000,
                "min_win_rate": 0.55,
                "hold_time_minutes": [30, 10080],
                "min_trades_90d": 20,
                "min_wallet_age_days": 60,
                "target_pool_size": [WALLET_POOL_TARGET_MIN, WALLET_POOL_TARGET_MAX],
            },
        },
        "wallets": final,
    }
    WALLET_POOL_PATH.write_text(json.dumps(pool_doc, indent=2) + "\n")
    print(f"Wrote {len(final)} wallets to {WALLET_POOL_PATH}")
    return 0


def _to_entry(stats: WalletStats) -> dict:
    return {
        "address": stats.address,
        "chain": stats.chain,
        "pnl_usd_180d": stats.pnl_usd_180d,
        "win_rate": stats.win_rate,
        "avg_hold_minutes": stats.avg_hold_minutes,
        "trade_count_90d": stats.trade_count_90d,
        "wallet_age_days": stats.wallet_age_days,
    }


def main() -> None:
    configure_logging()
    log = get_logger("scripts.curate_wallet_pool")

    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Print summary, don't write file")
    g.add_argument("--apply", action="store_true", help="Overwrite wallet_pool.json")
    args = p.parse_args()
    rc = asyncio.run(curate(dry_run=args.dry_run, log=log))
    sys.exit(rc)


if __name__ == "__main__":
    main()
