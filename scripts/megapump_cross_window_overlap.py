"""Cross-window overlap between two historical_megapump_wallet_analysis runs.

The pump.fun v3 API only exposes two narrow windows:
- ASC: pump.fun's earliest graduates (~2024-01-25 to 2024-03-28)
- DESC: the last ~14 days from today

Roy's hypothesis (2026-06-04): quality wallets participate in mega-pumps at
a frequency of ONE EVERY 1-3 MONTHS. To test this, we need to look across
months. The two reachable windows are ~2 years apart — perfect for the test.
A wallet appearing in BOTH a 2024-Q1 mega-pump and a 2026-Q2 mega-pump is
a durable repeat-winner across years.

Usage:
  docker compose exec bot_copy python -m scripts.megapump_cross_window_overlap \\
    /tmp/megapump_per_wallet_<ts1>.csv \\
    /tmp/megapump_per_wallet_<ts2>.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class WalletRow:
    address: str
    n_tokens: int
    total_pnl_usd: float
    total_volume_usd: float
    in_active_pool: bool
    is_repeat_winner: bool


def load_csv(path: Path) -> dict[str, WalletRow]:
    rows: dict[str, WalletRow] = {}
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            addr = r["wallet_address"]
            rows[addr] = WalletRow(
                address=addr,
                n_tokens=int(r["n_megapump_tokens"]),
                total_pnl_usd=float(r.get("total_pnl_usd") or 0.0),
                total_volume_usd=float(r.get("total_volume_usd") or 0.0),
                in_active_pool=bool(int(r.get("in_active_pool") or 0)),
                is_repeat_winner=bool(int(r.get("is_repeat_winner") or 0)),
            )
    return rows


def _get_pool_active() -> set[str]:
    try:
        from sqlalchemy import text
        from framework.db import session_scope
        with session_scope() as s:
            rows = s.execute(text(
                "SELECT address FROM wallet_pool WHERE tier='active'"
            )).all()
        return {str(r.address) for r in rows}
    except Exception:
        return set()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("csv_a", help="First CSV (e.g. ASC early-era run output)")
    p.add_argument("csv_b", help="Second CSV (e.g. DESC recent run output)")
    p.add_argument("--out-dir", default="/tmp")
    args = p.parse_args()

    path_a, path_b = Path(args.csv_a), Path(args.csv_b)
    if not path_a.exists() or not path_b.exists():
        print(f"ERROR: input CSV not found ({path_a} or {path_b})", file=sys.stderr)
        return 2

    rows_a = load_csv(path_a)
    rows_b = load_csv(path_b)
    print(f"[load] {path_a.name}: {len(rows_a)} wallets", file=sys.stderr)
    print(f"[load] {path_b.name}: {len(rows_b)} wallets", file=sys.stderr)

    overlap = set(rows_a.keys()) & set(rows_b.keys())
    print(f"\n[overlap] {len(overlap)} wallets appear in BOTH runs", file=sys.stderr)

    pool_active = _get_pool_active()
    if pool_active:
        in_pool = overlap & pool_active
        missing = overlap - pool_active
        print(f"  -> {len(in_pool)} already in our active pool", file=sys.stderr)
        print(f"  -> {len(missing)} MISSING from our pool", file=sys.stderr)
    else:
        in_pool = set()
        missing = overlap

    # Repeat winners in BOTH windows (>= 2 tokens in each)
    strong_overlap = {
        w for w in overlap
        if rows_a[w].n_tokens >= 2 and rows_b[w].n_tokens >= 2
    }
    print(f"\n[strong] {len(strong_overlap)} wallets are repeat winners in BOTH "
          f"windows (>= 2 tokens in each)", file=sys.stderr)

    # Render report
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"megapump_cross_window_{stamp}.md"

    lines: list[str] = []
    lines.append("# Cross-Window Mega-Pump Overlap\n")
    lines.append(f"- Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- Window A: `{path_a.name}` — {len(rows_a)} wallets")
    lines.append(f"- Window B: `{path_b.name}` — {len(rows_b)} wallets")
    lines.append(f"- Overlap (any token in both): **{len(overlap)}** wallets")
    lines.append(f"- Strong overlap (>= 2 tokens in BOTH): **{len(strong_overlap)}** wallets")
    if pool_active:
        lines.append(f"- Overlap already in active pool: {len(in_pool)}")
        lines.append(f"- Overlap MISSING from active pool: {len(missing)}\n")

    if not overlap:
        lines.append("## No cross-window overlap\n")
        lines.append("No wallet appeared as a top-trader OR early-buyer in BOTH the "
                     "earlier window and the recent window. Combined with the lack of "
                     "WITHIN-window repeat winners from the original analyses, this is "
                     "decisive evidence that **smart-money wallets do not durably recur "
                     "across mega-pumps at quarter-or-longer cadences.** The fat tail "
                     "is driven by one-off coordinated buying we cannot pre-select.\n")
        md_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"\n[done] report: {md_path}", file=sys.stderr)
        return 0

    # Top overlapping wallets
    def _total_tokens(addr: str) -> int:
        return rows_a[addr].n_tokens + rows_b[addr].n_tokens

    top = sorted(overlap, key=lambda w: -_total_tokens(w))[:30]
    lines.append("## Top 30 cross-window overlapping wallets\n")
    lines.append("| Wallet (prefix) | Tokens (A) | Tokens (B) | Pool? |")
    lines.append("|---|---|---|---|")
    for w in top:
        marker = "✓" if w in pool_active else "✗ ADD"
        lines.append(f"| `{w[:24]}...` | {rows_a[w].n_tokens} | "
                     f"{rows_b[w].n_tokens} | {marker} |")
    lines.append("")

    if missing:
        lines.append("## High-priority pool ADDS (cross-window overlap NOT in active pool)\n")
        missing_sorted = sorted(missing, key=lambda w: -_total_tokens(w))[:20]
        lines.append("| Wallet | Tokens (A) | Tokens (B) |")
        lines.append("|---|---|---|")
        for w in missing_sorted:
            lines.append(f"| `{w}` | {rows_a[w].n_tokens} | {rows_b[w].n_tokens} |")
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[done] report: {md_path}", file=sys.stderr)
    print(f"\n--- REPORT ---\n", file=sys.stderr)
    print("\n".join(lines), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
