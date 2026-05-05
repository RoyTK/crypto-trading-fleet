"""Curate the COPY bot's wallet pool: Birdeye discovery + (optional) Cielo validation.

Three complementary input sources, all merge + dedup before validation:

1. **Hand-seeds** (always loaded from `bots/copy/wallet_seeds.json`).
2. **Birdeye gainers/losers leaderboard** (`--from-birdeye N`).
3. **Birdeye per-token top traders** (`--from-birdeye-tokens ADDR1,ADDR2,...`).

Validation modes:

- DEFAULT (Cielo): each candidate validated via Cielo's `/{wallet}/trading-stats`
  (30 credits each). Only ~5% of Birdeye top traders are in Cielo's index, so
  most candidates return "wallet_not_tracked" — pool ends up small (~10-15 from
  300 candidates). Use this for HIGH-CONFIDENCE pool curation.

- `--skip-cielo`: skips Cielo entirely. Filters on Birdeye's `trade_count`
  field directly. Yields a much larger pool (~150-200 from 300 candidates) at
  lower confidence. Use this for INITIAL Build A paper testing — we need a
  pool large enough for cluster signals to fire (3+ wallets per 15 min on the
  same token requires a big enough pool that random coincidence is plausible).

Build A is Solana-only.

Cost preview:
    Default Cielo path: ~30 credits per candidate (cap at ~9k credits / 18% budget for 300)
    --skip-cielo path:  zero Cielo credits; Birdeye is free-tier
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


async def curate(
    *,
    dry_run: bool,
    from_birdeye: int,
    from_birdeye_tokens: list[str],
    per_token_count: int,
    skip_cielo: bool,
    log,
) -> int:
    cielo: Optional[CieloClient] = None
    if not skip_cielo:
        cielo = CieloClient()
        if not cielo.settings.cielo_api_key:
            print("ERROR: CIELO_API_KEY not configured. "
                  "Use --skip-cielo to bypass validation.", file=sys.stderr)
            return 2

    # ---- Source 1: hand-seeds -------------------------------------------
    seeds = _load_seeds()
    in_scope_seeds = [s for s in seeds if s.get("chain") in BUILD_A_CHAINS]
    out_of_scope = [s for s in seeds if s.get("chain") not in BUILD_A_CHAINS]
    print(f"Hand-seeds: {len(seeds)} total, {len(in_scope_seeds)} in-scope (Solana)")
    if out_of_scope:
        print(f"  skipped non-Solana chains: {sorted({s.get('chain') for s in out_of_scope})}")

    # ---- Source 2 & 3: Birdeye discovery --------------------------------
    discovered: list[dict] = []
    need_birdeye = from_birdeye > 0 or from_birdeye_tokens
    birdeye: Optional[BirdeyeClient] = None
    if need_birdeye:
        birdeye = BirdeyeClient()
        if not birdeye.settings.birdeye_api_key:
            print("WARNING: BIRDEYE_API_KEY not set; skipping Birdeye discovery",
                  file=sys.stderr)
            birdeye = None

    if birdeye is not None:
        async with aiohttp.ClientSession() as session:
            # Source 2: global gainers/losers leaderboard
            if from_birdeye > 0:
                print(f"Birdeye gainers/losers: fetching top {from_birdeye} Solana traders...")
                traders = await birdeye.gainers_losers(
                    session, chain="solana", target_count=from_birdeye,
                )
                print(f"  returned {len(traders)} candidates")
                for t in traders:
                    discovered.append({
                        "address": t.address,
                        "chain": "solana",
                        "birdeye_pnl_usd": t.pnl_usd,
                        "birdeye_volume_usd": t.volume_usd,
                        "birdeye_trade_count": t.trade_count,
                        "note": f"birdeye gainers_losers 1W (pnl=${t.pnl_usd or 0:.0f})",
                    })

            # Source 3: per-token top traders for hot tokens
            if from_birdeye_tokens:
                print(f"Birdeye per-token: fetching top {per_token_count} traders for "
                      f"{len(from_birdeye_tokens)} token(s)...")
                for token_addr in from_birdeye_tokens:
                    token_short = f"{token_addr[:6]}...{token_addr[-4:]}"
                    print(f"  [{token_short}] ", end="", flush=True)
                    traders = await birdeye.token_top_traders(
                        session, token_address=token_addr, chain="solana",
                        time_frame="24h", sort_by="total_pnl",
                        target_count=per_token_count,
                    )
                    print(f"returned {len(traders)} traders")
                    for t in traders:
                        discovered.append({
                            "address": t.address,
                            "chain": "solana",
                            "birdeye_pnl_usd": t.pnl_usd,
                            "birdeye_volume_usd": t.volume_usd,
                            "birdeye_trade_count": t.trade_count,
                            "note": f"birdeye top_traders {token_short} 24h_pnl "
                                    f"(pnl=${t.pnl_usd or 0:.0f})",
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
    print(f"\nTotal unique candidates: {len(merged)}")
    if skip_cielo:
        print("  validation: --skip-cielo (Birdeye trade_count only, no API cost)\n")
    else:
        print(f"  validation: Cielo /trading-stats (~{len(merged) * 30} credits, "
              f"{len(merged) * 30 / 50_000 * 100:.1f}% of monthly Pro budget)\n")

    if not merged:
        print("ERROR: no candidates. Add hand-seeds OR use --from-birdeye N "
              "OR --from-birdeye-tokens TOKEN1,TOKEN2,...", file=sys.stderr)
        return 2

    passed: list[dict] = []
    rejected: list[dict] = []
    errors: list[dict] = []

    if skip_cielo:
        # ---- Birdeye-only validation (Path 1) --------------------------
        # Filter solely on Birdeye trade_count to weed out single-trade luck.
        # Hand-seeds without Birdeye stats pass through (trust the human).
        from bots.copy.config import WALLET_MIN_TRADES_90D
        for cand in merged:
            note = cand.get("note", "")
            tc = cand.get("birdeye_trade_count")
            if tc is None:
                # Hand-seeded entry — accept as-is
                passed.append({
                    "address": cand["address"],
                    "chain": cand["chain"],
                    "birdeye_pnl_usd": None,
                    "birdeye_volume_usd": None,
                    "birdeye_trade_count": None,
                    "note": note or "hand-seed",
                })
                continue
            if tc < WALLET_MIN_TRADES_90D:
                rejected.append({
                    "address": cand["address"], "chain": cand["chain"],
                    "note": note,
                    "reasons": [f"birdeye_trade_count_below_{WALLET_MIN_TRADES_90D}"],
                })
                continue
            passed.append({
                "address": cand["address"],
                "chain": cand["chain"],
                "birdeye_pnl_usd": cand.get("birdeye_pnl_usd"),
                "birdeye_volume_usd": cand.get("birdeye_volume_usd"),
                "birdeye_trade_count": tc,
                "note": note,
            })
        # Sort by Birdeye pnl × volume — if pnl missing (hand-seed), put last
        passed.sort(
            key=lambda e: (e.get("birdeye_pnl_usd") or 0) * (e.get("birdeye_volume_usd") or 0),
            reverse=True,
        )
    else:
        # ---- Cielo-validated path --------------------------------------
        # Cielo Pro rate limit: 25 credits/sec, /trading-stats = 30 credits/call.
        CIELO_CALL_INTERVAL_SECONDS = 1.3
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
                else:
                    ok, reasons = passes_curation_filters(stats)
                    entry = _to_entry(stats, note)
                    if ok:
                        passed.append(entry)
                        print(f"OK pnl=${stats.pnl_usd:.0f} wr={stats.win_rate:.2f} "
                              f"swaps={stats.swap_count}")
                    else:
                        rejected.append({"address": address, "chain": chain, "note": note,
                                         "stats": entry, "reasons": reasons})
                        print(f"REJECT {reasons}")
                if i < len(merged):
                    await asyncio.sleep(CIELO_CALL_INTERVAL_SECONDS)
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
        print("\nTop 10 by quality:")
        for e in passed[:10]:
            if skip_cielo:
                pnl = e.get("birdeye_pnl_usd") or 0
                tc = e.get("birdeye_trade_count") or 0
                print(f"  {e['chain']:8s} {e['address'][:8]}...{e['address'][-6:]}"
                      f"  birdeye_pnl=${pnl:>10.0f}  trades={tc:>6}  "
                      f"note={e.get('note', '')[:50]}")
            else:
                print(f"  {e['chain']:8s} {e['address'][:8]}...{e['address'][-6:]}"
                      f"  pnl=${e['pnl_usd']:>9.0f}  wr={e['win_rate']:.2f}  "
                      f"swaps={e['swap_count']}  note={e.get('note', '')[:40]}")

    write_audit(
        "copy_wallet_pool_curation",
        bot_id="copy",
        payload={
            "skip_cielo": skip_cielo,
            "from_birdeye": from_birdeye,
            "from_birdeye_tokens": from_birdeye_tokens,
            "per_token_count": per_token_count,
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
            "version": 2,
            "curated_at": datetime.now(timezone.utc).isoformat(),
            "source": "scripts/curate_wallet_pool.py",
            "build": "A_solana_only",
            "validation_mode": "birdeye_only" if skip_cielo else "cielo",
            "inputs": {
                "from_birdeye": from_birdeye,
                "from_birdeye_tokens": from_birdeye_tokens,
                "per_token_count": per_token_count,
                "hand_seed_count": len(in_scope_seeds),
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
    p = argparse.ArgumentParser(
        description="Curate the COPY bot's wallet pool from hand-seeds + Birdeye discovery.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Hand-seeds only (no Birdeye)
  python -m scripts.curate_wallet_pool --dry-run

  # Add 100 global gainers/losers
  python -m scripts.curate_wallet_pool --from-birdeye 100 --dry-run

  # Add top traders for specific hot tokens (50 per token by default)
  python -m scripts.curate_wallet_pool --from-birdeye-tokens \\
    25hAyBQfoDhfWx9ay6rarbgvWGwDdNqcHsXS3jQ3mTDJ,\\
    9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump --dry-run

  # Combined: leaderboard + per-token + hand-seeds
  python -m scripts.curate_wallet_pool \\
    --from-birdeye 50 \\
    --from-birdeye-tokens TOKEN1,TOKEN2 \\
    --per-token-count 30 --apply
        """)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="Validate candidates and print summary; do NOT write wallet_pool.json")
    g.add_argument("--apply", action="store_true",
                   help="Validate candidates and overwrite wallet_pool.json")
    p.add_argument("--from-birdeye", type=int, default=0, metavar="N",
                   help="Discover top N Solana traders from Birdeye gainers/losers leaderboard. "
                        "Default 0 = skip leaderboard.")
    p.add_argument("--from-birdeye-tokens", type=str, default="",
                   metavar="ADDR1,ADDR2,...",
                   help="Comma-separated list of Solana token mint addresses. For each token, "
                        "fetches top traders by 24h total_pnl from Birdeye's /defi/v2/tokens/top_traders. "
                        "Use --per-token-count to control how many per token (default 50).")
    p.add_argument("--per-token-count", type=int, default=50, metavar="N",
                   help="Top N traders to fetch per token (used with --from-birdeye-tokens). Default 50.")
    p.add_argument("--skip-cielo", action="store_true",
                   help="Skip Cielo /trading-stats validation. Filter only on Birdeye trade_count "
                        "(≥20). Yields a much larger pool at lower confidence. Recommended for "
                        "Build A initial paper testing where pool size matters more than precision.")
    args = p.parse_args()
    tokens = [t.strip() for t in args.from_birdeye_tokens.split(",") if t.strip()]
    rc = asyncio.run(curate(
        dry_run=args.dry_run,
        from_birdeye=args.from_birdeye,
        from_birdeye_tokens=tokens,
        per_token_count=args.per_token_count,
        skip_cielo=args.skip_cielo,
        log=log,
    ))
    sys.exit(rc)


if __name__ == "__main__":
    main()
