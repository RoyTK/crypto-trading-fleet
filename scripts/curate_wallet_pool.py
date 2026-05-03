"""Curate the COPY bot's wallet pool: Birdeye discovery + hand-seed → Cielo validation.

Two complementary input sources:
1. **Birdeye** (`--from-birdeye N`) — pull top N Solana traders from
   /trader/gainers-losers (free Standard tier endpoint). Quarterly automated
   discovery. Roy can opt in/out.
2. **Hand-seeds** (always loaded if `bots/copy/wallet_seeds.json` has entries) —
   wallets Roy adds manually from Twitter alpha accounts, Birdeye paywalled
   pages, alpha groups, DeBank, etc. Sources Birdeye's API can't reach or
   that benefit from Roy's judgment.

Both sets are merged + dedup'd, then EACH wallet is validated via Cielo's
`/{wallet}/trading-stats` endpoint (30 credits each). Only candidates that
pass all locked Item #7 filters are written to wallet_pool.json.

Build A is Solana-only. EVM joins in Build B.

Usage:
    # Hand-seeds only:
    python -m scripts.curate_wallet_pool --dry-run

    # Birdeye discovery + hand-seeds:
    python -m scripts.curate_wallet_pool --from-birdeye 100 --dry-run

    # Apply (writes wallet_pool.json):
    python -m scripts.curate_wallet_pool --from-birdeye 100 --apply

Cost preview:
    Birdeye discovery: ~10-20 free Standard CUs for 100 wallets
    Cielo validation: 30 credits × N wallets (e.g. 100 wallets = 3,000 credits = 6% of monthly budget)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp

from bots.copy.venue.birdeye import BirdeyeClient
from bots.copy.venue.cielo import CieloClient, WalletStats, passes_curation_filters
from framework.audit import write_audit
from framework.logging_setup import configure_logging, get_logger


REPO_ROOT = Path(__file__).resolve().parent.parent
WALLET_POOL_PATH = REPO_ROOT / "bots" / "copy" / "wallet_pool.json"
WALLET_SEEDS_PATH = REPO_ROOT / "bots" / "copy" / "wallet_seeds.json"

# Build A: Solana-only.
BUILD_A_CHAINS = {"solana"}


async def curate(*, dry_run: bool, from_birdeye: int, log) -> int:
    cielo = CieloClient()
    if not cielo.settings.cielo_api_key:
        print("ERROR: CIELO_API_KEY not configured. Aborting.", file=sys.stderr)
        return 2

    # ---- Source 1: hand-seeds -------------------------------------------
    seeds = _load_seeds()
    in_scope_seeds = [s for s in seeds if s.get("chain") in BUILD_A_CHAINS]
    out_of_scope = [s for s in seeds if s.get("chain") not in BUILD_A_CHAINS]
    print(f"Hand-seeds: {len(seeds)} total, {len(in_scope_seeds)} in-scope (Solana)")
    if out_of_scope:
        print(f"  skipped non-Solana chains: {sorted({s.get('chain') for s in out_of_scope})}")

    # ---- Source 2: Birdeye top traders ----------------------------------
    discovered: list[dict] = []
    if from_birdeye > 0:
        birdeye = BirdeyeClient()
        if not birdeye.settings.birdeye_api_key:
            print("WARNING: BIRDEYE_API_KEY not set; skipping Birdeye discovery",
                  file=sys.stderr)
        else:
            print(f"Birdeye discovery: fetching top {from_birdeye} Solana traders...")
            async with aiohttp.ClientSession() as session:
                traders = await birdeye.gainers_losers(
                    session, chain="solana", target_count=from_birdeye,
                )
            print(f"  Birdeye returned {len(traders)} candidates")
            for t in traders:
                discovered.append({
                    "address": t.address,
                    "chain": "solana",
                    "note": f"birdeye gainers_losers 1W (pnl=${t.pnl_usd or 0:.0f})",
                })

    # ---- Merge + dedup ---------------------------------------------------
    seen: set[tuple[str, str]] = set()
    merged: list[dict] = []
    for s in in_scope_seeds:
        key = (s["address"], s["chain"])
        if key not in seen:
            seen.add(key)
            merged.append(s)
    for d in discovered:
        key = (d["address"], d["chain"])
        if key not in seen:
            seen.add(key)
            merged.append(d)
    print(f"\nTotal unique candidates to validate: {len(merged)}")
    print(f"  Cielo cost preview: {len(merged) * 30} credits "
          f"({len(merged) * 30 / 50_000 * 100:.1f}% of monthly Pro budget)\n")

    if not merged:
        print("ERROR: no candidates to validate. Add hand-seeds OR use --from-birdeye N.",
              file=sys.stderr)
        return 2

    # ---- Validate each via Cielo /trading-stats -------------------------
    passed: list[dict] = []
    rejected: list[dict] = []
    errors: list[dict] = []
    async with aiohttp.ClientSession() as session:
        for i, cand in enumerate(merged, 1):
            address = cand["address"]
            chain = cand["chain"]
            note = cand.get("note", "")
            print(f"[{i}/{len(merged)}] {chain} {address[:8]}...{address[-6:]} ",
                  end="", flush=True)
            stats = await cielo.wallet_trading_stats(session, address, chain, days="max")
            if stats is None:
                print("ERROR (no stats)")
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
                print(f"REJECT {reasons}")

    # Sort by pnl × winrate
    passed.sort(key=lambda e: e["pnl_usd"] * e["win_rate"], reverse=True)

    # ---- Summary --------------------------------------------------------
    print()
    print("=" * 60)
    print(f"Validated:  {len(merged)}")
    print(f"Passed:     {len(passed)}")
    print(f"Rejected:   {len(rejected)}")
    print(f"Errors:     {len(errors)}")
    if rejected:
        reason_counts: Counter = Counter()
        for r in rejected:
            for reason in r["reasons"]:
                reason_counts[reason] += 1
        print("\nRejection reasons:")
        for reason, n in reason_counts.most_common():
            print(f"  {reason}: {n}")
    if passed:
        print("\nTop 10 by pnl × winrate:")
        for e in passed[:10]:
            print(f"  {e['chain']:8s} {e['address'][:8]}...{e['address'][-6:]}"
                  f"  pnl=${e['pnl_usd']:>9.0f}  wr={e['win_rate']:.2f}  "
                  f"swaps={e['swap_count']}  note={e.get('note', '')[:40]}")

    write_audit(
        "copy_wallet_pool_curation",
        bot_id="copy",
        payload={
            "from_birdeye": from_birdeye,
            "hand_seed_count": len(in_scope_seeds),
            "discovered_count": len(discovered),
            "validated_count": len(merged),
            "passed_count": len(passed),
            "rejected_count": len(rejected),
            "error_count": len(errors),
            "dry_run": dry_run,
            "build": "A_solana_only",
        },
    )

    if dry_run:
        print("\n--dry-run: not writing wallet_pool.json. Re-run with --apply to commit.")
        return 0

    pool_doc = {
        "metadata": {
            "version": 1,
            "curated_at": datetime.now(timezone.utc).isoformat(),
            "source": "scripts/curate_wallet_pool.py",
            "build": "A_solana_only",
            "inputs": {
                "from_birdeye": from_birdeye,
                "hand_seed_count": len(in_scope_seeds),
            },
            "criteria": {
                "min_pnl_usd": 50000,
                "min_win_rate": 0.55,
                "hold_time_minutes": [30, 10080],
                "min_swap_count": 20,
                "min_consecutive_trading_days": 30,
            },
            "stats": {
                "validated": len(merged),
                "passed": len(passed),
                "rejected": len(rejected),
                "errors": len(errors),
            },
        },
        "wallets": passed,
    }
    WALLET_POOL_PATH.write_text(json.dumps(pool_doc, indent=2) + "\n")
    print(f"\nWrote {len(passed)} wallets to {WALLET_POOL_PATH}")
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
    g.add_argument("--dry-run", action="store_true",
                   help="Validate candidates and print summary; do NOT write wallet_pool.json")
    g.add_argument("--apply", action="store_true",
                   help="Validate candidates and overwrite wallet_pool.json")
    p.add_argument("--from-birdeye", type=int, default=0,
                   metavar="N",
                   help="Discover top N Solana traders from Birdeye (free Standard tier endpoint). "
                        "Default 0 = hand-seeds only.")
    args = p.parse_args()
    rc = asyncio.run(curate(
        dry_run=args.dry_run,
        from_birdeye=args.from_birdeye,
        log=log,
    ))
    sys.exit(rc)


if __name__ == "__main__":
    main()
