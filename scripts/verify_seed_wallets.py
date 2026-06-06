"""Verify a seed CSV from browser-Opus's mega-pump cross-recurrence analysis.

Pipeline:
1. Ingest CSV (any format with a `wallet_address` column). Optional
   `token_symbol` column lets us aggregate cross-token recurrence per wallet.
2. Dedup against `wallet_pool` table: separate "already curated" wallets from
   "new candidates" needing verification.
3. For each NEW candidate:
   - Cielo `/trading-stats` (30 credits/call): PnL, winrate, swap count,
     average hold time, consecutive active days
   - Apply COPY's existing `passes_curation_filters()` (winrate >= 55%
     AND swap_count >= 20)
   - Helius enhanced-transactions: identify the wallet's FIRST inbound SOL
     transfer to find its funding source. Wallets sharing a funder are likely
     bundled (single operator) and should collapse to one entity.
4. Output ranked markdown report + CSV.

Cost: 30 Cielo credits per new candidate + 1 Helius credit per candidate.
For 10 candidates that's ~300 Cielo credits (0.6% of monthly Pro quota).

Output:
  /tmp/verify_seed_<ts>.md   — human-readable report
  /tmp/verify_seed_<ts>.csv  — machine-readable per-wallet result

Usage:
  # CSV input (browser-Opus format)
  docker compose exec bot_copy python -m scripts.verify_seed_wallets <csv_path>

  # Address list mode (one address per line)
  docker compose exec bot_copy python -m scripts.verify_seed_wallets - --addresses <file>
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from sqlalchemy import text

from bots.copy.venue.cielo import CieloClient, WalletStats, passes_curation_filters
from framework.db import session_scope


HELIUS_BASE = "https://api.helius.xyz"


@dataclass
class SeedRow:
    wallet_address: str
    tokens: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def recurrence_count(self) -> int:
        return len(set(self.tokens))


def load_seed_csv(path: Path) -> list[SeedRow]:
    """Load CSV; aggregate multi-row-per-wallet inputs by wallet_address."""
    by_wallet: dict[str, set[str]] = defaultdict(set)
    extras_by_wallet: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            w = (r.get("wallet_address") or "").strip()
            if not w:
                continue
            tok = (r.get("token_symbol") or "").strip()
            if tok and tok not in ("multi", "RECUR3", "RECUR4", "RECUR5",
                                    "RECUR6", "RECUR7", "RECUR3_HFT"):
                by_wallet[w].add(tok)
            elif tok and tok.startswith("RECUR"):
                # Browser-Opus's collapsed format: one row per wallet with
                # token_symbol="RECURn" + token_address="multi". The actual
                # token list isn't in the CSV — fall back to recurrence count
                # from the n in RECURn.
                extras_by_wallet[w] = {"recur_label": tok}
            if w not in extras_by_wallet:
                extras_by_wallet[w] = {}
            for k in ("is_bundled", "bundle_id"):
                v = (r.get(k) or "").strip()
                if v and v != "NULL":
                    extras_by_wallet[w][k] = v
    out = []
    for w, toks in by_wallet.items():
        out.append(SeedRow(wallet_address=w, tokens=sorted(toks),
                            extra=extras_by_wallet.get(w, {})))
    # Include wallets that had ONLY recurrence-label rows (no token list)
    for w, ex in extras_by_wallet.items():
        if w not in by_wallet:
            out.append(SeedRow(wallet_address=w, tokens=[], extra=ex))
    return out


def load_addresses_txt(path: Path) -> list[SeedRow]:
    """Plain-text mode: one wallet address per line."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            addr = line.strip()
            if addr and not addr.startswith("#"):
                out.append(SeedRow(wallet_address=addr))
    return out


def load_recurring_tsv(path: Path) -> list[SeedRow]:
    """Browser-Opus recurring-wallets TSV format.

    Expected columns: count, wallet_address, tokens_touched
    tokens_touched is pipe-separated (e.g. "GOAT|ACT|MELANIA").
    """
    out: list[SeedRow] = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for r in reader:
            w = (r.get("wallet_address") or "").strip()
            if not w:
                continue
            toks_field = (r.get("tokens_touched") or "").strip()
            toks = [t.strip() for t in toks_field.split("|") if t.strip()]
            out.append(SeedRow(wallet_address=w, tokens=toks))
    return out


def get_pool_wallets() -> dict[str, str]:
    """Returns {address: tier} for ALL wallet_pool entries (active/watch/pruned)."""
    try:
        with session_scope() as s:
            rows = s.execute(text("SELECT address, tier FROM wallet_pool")).all()
        return {str(r.address): str(r.tier) for r in rows}
    except Exception as e:
        print(f"  [warn] wallet_pool query failed: {e}", file=sys.stderr)
        return {}


async def fetch_funding_source(
    session: aiohttp.ClientSession, address: str, helius_key: str,
) -> Optional[str]:
    """Find the FIRST inbound SOL transfer to `address` and return its sender.

    Wallets sharing a funder are likely controlled by the same operator
    (bundle dedup). Returns None if Helius doesn't surface the funding tx
    or if the wallet was funded via SPL token transfer (not native SOL).
    """
    url = (f"{HELIUS_BASE}/v0/addresses/{address}/transactions"
           f"?api-key={helius_key}&limit=50")
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return None
            txs = await r.json()
    except Exception:
        return None
    if not isinstance(txs, list) or not txs:
        return None
    # Sort ASC by timestamp; find earliest inbound native-SOL transfer
    txs_sorted = sorted(txs, key=lambda t: t.get("timestamp") or 0)
    for tx in txs_sorted:
        for nt in (tx.get("nativeTransfers") or []):
            if nt.get("toUserAccount") == address and (nt.get("amount") or 0) > 0:
                src = nt.get("fromUserAccount")
                if src and src != address:
                    return src
    return None


async def verify_one(
    session: aiohttp.ClientSession,
    cielo: CieloClient,
    helius_key: str,
    seed: SeedRow,
) -> dict:
    """Verify one new candidate wallet."""
    stats = await cielo.wallet_trading_stats(
        session, seed.wallet_address, chain="solana", days="max",
    )
    funder = await fetch_funding_source(session, seed.wallet_address, helius_key)
    base = {
        "wallet": seed.wallet_address,
        "tokens": ",".join(seed.tokens) if seed.tokens else "",
        "recurrence": seed.recurrence_count,
        "recur_label": seed.extra.get("recur_label", ""),
        "funder": funder or "",
    }
    if stats is None:
        return {
            **base,
            "cielo_pnl": None, "cielo_winrate": None, "cielo_swaps": None,
            "cielo_hold_min": None, "cielo_active_days": None,
            "passes": False, "note": "cielo_not_tracked",
        }
    ok, reasons = passes_curation_filters(stats)
    return {
        **base,
        "cielo_pnl": stats.pnl_usd,
        "cielo_winrate": stats.win_rate,
        "cielo_swaps": stats.swap_count,
        "cielo_hold_min": stats.avg_hold_minutes,
        "cielo_active_days": stats.consecutive_trading_days,
        "passes": ok,
        "note": ",".join(reasons) if reasons else "passes_filters",
    }


def render_markdown(
    args, seeds: list[SeedRow], in_pool: list[SeedRow], pool_tiers: dict[str, str],
    results: list[dict], bundles: dict[str, list[str]],
) -> str:
    lines: list[str] = []
    lines.append("# Seed Wallet Verification Report\n")
    lines.append(f"- Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- Seed source: `{args.csv}` (mode: {args.mode})")
    lines.append(f"- Total seed wallets: {len(seeds)}")
    lines.append(f"- Already in our wallet_pool: {len(in_pool)}")
    lines.append(f"- New candidates verified: {len(results)}")
    passed_n = sum(1 for r in results if r["passes"])
    lines.append(f"- New candidates PASSING curation filters: **{passed_n}**")
    multi_bundles = {k: v for k, v in bundles.items() if len(v) > 1}
    lines.append(f"- Bundle clusters (shared funder, ≥2 wallets): {len(multi_bundles)}\n")

    if in_pool:
        lines.append("## Already in our wallet_pool\n")
        lines.append("These were caught by past curation — validates that some seed wallets DO "
                     "match our existing criteria, but check their TIER (active vs watch vs pruned).\n")
        lines.append("| Wallet | Tier | Tokens touched in seed | Recurrence |")
        lines.append("|---|---|---|---|")
        for s in sorted(in_pool, key=lambda s: -s.recurrence_count):
            tier = pool_tiers.get(s.wallet_address, "?")
            toks = ",".join(s.tokens) if s.tokens else s.extra.get("recur_label", "?")
            lines.append(f"| `{s.wallet_address[:24]}...` | {tier} | {toks} | {s.recurrence_count} |")
        lines.append("")

    passed = sorted(
        [r for r in results if r["passes"]],
        key=lambda r: (-r["recurrence"], -(r.get("cielo_pnl") or 0)),
    )
    if passed:
        lines.append(f"## NEW candidates PASSING curation ({len(passed)})\n")
        lines.append("**Recommended action:** promote to active tier after spot-checking bundle "
                     "membership below. Each appears in multiple historical 100x+ tokens AND "
                     "passes COPY's existing winrate/swap-count filters.\n")
        lines.append("| Wallet | Recur | Tokens | Cielo PnL $ | WR | Swaps | Hold min | "
                     "Active d | Funder |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for r in passed:
            pnl_s = f"{r['cielo_pnl']:,.0f}" if r.get("cielo_pnl") is not None else "?"
            wr_s = f"{r['cielo_winrate']:.2f}" if r.get("cielo_winrate") is not None else "?"
            sw_s = str(r["cielo_swaps"]) if r.get("cielo_swaps") is not None else "?"
            hold_s = f"{r['cielo_hold_min']:.1f}" if r.get("cielo_hold_min") is not None else "?"
            act_s = str(r["cielo_active_days"]) if r.get("cielo_active_days") is not None else "?"
            funder_s = (r.get("funder") or "")[:16] + ("..." if r.get("funder") else "?")
            toks = r["tokens"] or r.get("recur_label", "?")
            lines.append(f"| `{r['wallet'][:24]}...` | {r['recurrence']} | {toks} | "
                         f"{pnl_s} | {wr_s} | {sw_s} | {hold_s} | {act_s} | `{funder_s}` |")
        lines.append("")

    failed = [r for r in results if not r["passes"]]
    if failed:
        lines.append(f"## NEW candidates NOT passing curation ({len(failed)})\n")
        lines.append("Either Cielo doesn't track them OR their winrate/swap-count fails our "
                     "filters. Worth manually reviewing the high-recurrence ones — Cielo coverage "
                     "isn't universal.\n")
        lines.append("| Wallet | Recur | Tokens | Note | Cielo PnL | WR | Swaps |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in sorted(failed, key=lambda r: -r["recurrence"]):
            pnl_s = f"{r['cielo_pnl']:,.0f}" if r.get("cielo_pnl") is not None else "—"
            wr_s = f"{r['cielo_winrate']:.2f}" if r.get("cielo_winrate") is not None else "—"
            sw_s = str(r["cielo_swaps"]) if r.get("cielo_swaps") is not None else "—"
            toks = r["tokens"] or r.get("recur_label", "?")
            lines.append(f"| `{r['wallet'][:24]}...` | {r['recurrence']} | {toks} | "
                         f"{r['note']} | {pnl_s} | {wr_s} | {sw_s} |")
        lines.append("")

    if multi_bundles:
        lines.append("## Bundle clusters detected (wallets sharing a funder)\n")
        lines.append("Wallets that received their FIRST inbound SOL from the SAME source. "
                     "Likely controlled by one operator — collapse to a single 'operator entity' "
                     "before counting cross-token recurrence (otherwise a single operator with N "
                     "wallets inflates recurrence by N).\n")
        for funder, wallets in sorted(multi_bundles.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"### Funder `{funder}` controls {len(wallets)} seed wallets")
            for w in wallets:
                lines.append(f"- `{w}`")
            lines.append("")

    lines.append("## Next steps\n")
    lines.append("1. For each PASSING new candidate not in a bundle: open a Solscan / GeckoTerminal "
                 "tab and spot-check. Sanity is cheap.")
    lines.append("2. Promote validated wallets via the existing curation flow "
                 "(`scripts/curate_wallet_pool.py` or direct UPDATE on `wallet_pool.tier`).")
    lines.append("3. For wallets ALREADY in pool: if any are at `watch` or `pruned`, the cross-pump "
                 "evidence justifies promotion to `active`.")
    lines.append("4. For bundle clusters: pick the wallet with highest Cielo PnL as the 'primary' "
                 "and skip the rest of the bundle.\n")
    return "\n".join(lines)


def write_csv(path: Path, results: list[dict]) -> None:
    if not results:
        return
    keys = list(results[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for r in results:
            writer.writerow(r)


async def main_async(args) -> int:
    seed_path = Path(args.csv) if args.csv != "-" else None
    if args.mode == "csv":
        if seed_path is None or not seed_path.exists():
            print(f"ERROR: CSV not found: {args.csv}", file=sys.stderr)
            return 2
        seeds = load_seed_csv(seed_path)
    elif args.mode == "tsv":
        tsv_path = Path(args.tsv) if args.tsv else None
        if tsv_path is None or not tsv_path.exists():
            print(f"ERROR: --tsv file not found", file=sys.stderr)
            return 2
        seeds = load_recurring_tsv(tsv_path)
    else:  # addresses mode
        addr_path = Path(args.addresses) if args.addresses else None
        if addr_path is None or not addr_path.exists():
            print(f"ERROR: --addresses file not found", file=sys.stderr)
            return 2
        seeds = load_addresses_txt(addr_path)

    if not seeds:
        print(f"ERROR: 0 seed wallets parsed from input", file=sys.stderr)
        return 1
    print(f"[load] {len(seeds)} seed wallets parsed", file=sys.stderr)

    # Apply min-recurrence filter (skip long-tail single-appearance wallets)
    if args.min_recurrence > 1:
        kept = [s for s in seeds if s.recurrence_count >= args.min_recurrence]
        dropped = len(seeds) - len(kept)
        print(f"  -> applying --min-recurrence {args.min_recurrence}: "
              f"kept {len(kept)}, dropped {dropped}", file=sys.stderr)
        seeds = kept

    pool = get_pool_wallets()
    new_seeds = [s for s in seeds if s.wallet_address not in pool]
    in_pool_seeds = [s for s in seeds if s.wallet_address in pool]
    print(f"  -> {len(in_pool_seeds)} already in wallet_pool (skip Cielo, surface tier)",
          file=sys.stderr)
    print(f"  -> {len(new_seeds)} NEW candidates to verify via Cielo + Helius",
          file=sys.stderr)
    print(f"  -> Cielo budget: ~{len(new_seeds) * 30} credits "
          f"({100 * len(new_seeds) * 30 / 50000:.1f}% of monthly 50k Pro quota)",
          file=sys.stderr)

    helius_key = os.environ.get("HELIUS_API_KEY", "")
    if not helius_key:
        print("  [warn] HELIUS_API_KEY not set — funder detection will be skipped",
              file=sys.stderr)
    cielo = CieloClient()

    results: list[dict] = []
    if new_seeds:
        async with aiohttp.ClientSession() as session:
            for i, seed in enumerate(new_seeds, 1):
                print(f"  [{i:>3}/{len(new_seeds)}] verifying {seed.wallet_address[:16]}...",
                      file=sys.stderr)
                res = await verify_one(session, cielo, helius_key, seed)
                results.append(res)
                # Cielo Pro: 25 credits/sec. trading-stats is 30 credits → 1.2s effective
                await asyncio.sleep(0.1)

    bundles: dict[str, list[str]] = defaultdict(list)
    for r in results:
        fs = r.get("funder") or ""
        if fs:
            bundles[fs].append(r["wallet"])

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"verify_seed_{stamp}.md"
    csv_path = out_dir / f"verify_seed_{stamp}.csv"

    md = render_markdown(args, seeds, in_pool_seeds, pool, results, bundles)
    md_path.write_text(md, encoding="utf-8")
    write_csv(csv_path, results)

    print(f"\n[done] report: {md_path}", file=sys.stderr)
    print(f"[done] csv:    {csv_path}", file=sys.stderr)
    print(f"\n--- REPORT ---\n", file=sys.stderr)
    print(md, file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", nargs="?", default="-",
                        help="Seed CSV path (or '-' to use --addresses or --tsv)")
    parser.add_argument("--addresses", default=None,
                        help="Text file with one Solana address per line")
    parser.add_argument("--tsv", default=None,
                        help="Browser-Opus recurring-wallets TSV (columns: count, "
                             "wallet_address, tokens_touched pipe-separated)")
    parser.add_argument("--min-recurrence", type=int, default=1,
                        help="Only verify wallets touching >= N seed tokens. Use 2+ to skip the "
                             "long tail of single-appearance wallets on large CSVs (saves Cielo "
                             "credits). Already-in-pool wallets are dedup'd regardless.")
    parser.add_argument("--solscan-tag-column", default="solscan_tag",
                        help="CSV column with Solscan whale tag (informational, passed through)")
    parser.add_argument("--out-dir", default="/tmp")
    args = parser.parse_args()

    if args.csv == "-" and args.tsv:
        args.mode = "tsv"
    elif args.csv == "-" and args.addresses:
        args.mode = "addresses"
    elif args.csv != "-":
        args.mode = "csv"
    else:
        print("ERROR: provide CSV path, --tsv <file>, or --addresses <file>", file=sys.stderr)
        return 2

    if not os.environ.get("CIELO_API_KEY"):
        print("ERROR: CIELO_API_KEY required in env", file=sys.stderr)
        return 1

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
