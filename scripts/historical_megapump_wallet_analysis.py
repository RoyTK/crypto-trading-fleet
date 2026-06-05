"""Historical mega-pump wallet analysis.

Tests the hypothesis: "the true edge is a small set of recurring wallets
that consistently catch 100x+ mega-pumps."

Pipeline:
1. Discover ~200 historical pump.fun graduated tokens (default window =
   pump.fun's earliest era so each has 60d+ of observable history).
2. Fetch 60d Birdeye history per token; compute max_multiple.
3. Filter to tokens with max_multiple >= --min-multiple (default 100x).
4. For each surviving mega-pump, fetch up to N top traders by PnL from
   Birdeye's /defi/v2/tokens/top_traders endpoint.
5. Aggregate: wallet → list[tokens_traded].
6. Identify "repeat winners" — wallets appearing in >= --repeat-threshold
   different mega-pumps. These are the proven multi-pump catchers.
7. Compare against COPY's active wallet pool: which repeat winners are
   already curated vs missing (= high-priority discovery targets).

Outputs:
  /tmp/megapump_repeat_winners_<ts>.md
  /tmp/megapump_per_wallet_<ts>.csv

Cost: ~50 mega-pumps × 5 Birdeye top-trader calls/token = 250 CU plus
200 history calls = ~450 CU total. Birdeye Lite is 30k CU/mo. Fine.

Roy's 2026-06-04 ask: "Can we look at historical data on other tokens
that had a 1000x+ run (or maybe there is validity in 100x+ runs) and
look for a pattern?" — this script is the answer.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


# Reuse the lifecycle script's tested helpers.
from scripts.pumpfun_lifecycle_analysis import (
    BIRDEYE_BASE,
    BIRDEYE_SLEEP_SECONDS,
    HISTORY_DAYS,
    USER_AGENT,
    _http_get_json,
    compute_features,
    discover_tokens,
    fetch_history_birdeye,
)


@dataclass
class TopTrader:
    wallet_address: str
    token_mint: str
    token_symbol: Optional[str]
    pnl_usd: Optional[float]
    volume_usd: Optional[float]
    trade_count: Optional[int]


@dataclass
class MegaPump:
    mint: str
    symbol: Optional[str]
    max_multiple: float
    final_multiple: float
    created_at: Optional[datetime]


def _safe_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _safe_int(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def fetch_top_traders_birdeye(
    mint: str,
    target_count: int = 50,
    time_frame: str = "alltime",
) -> list[TopTrader]:
    """Synchronous Birdeye top-traders fetch for a Solana token.

    Paginates 10 at a time (Birdeye's per-call cap on this endpoint).
    Stops on empty page or target_count reached.
    """
    api_key = os.environ.get("BIRDEYE_API_KEY")
    if not api_key:
        return []
    headers = {
        "X-API-KEY": api_key,
        "x-chain": "solana",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    out: list[TopTrader] = []
    offset = 0
    page_size = 10
    while len(out) < target_count and offset < 500:
        params = {
            "address": mint,
            "time_frame": time_frame,
            "sort_by": "PnL",
            "sort_type": "desc",
            "offset": str(offset),
            "limit": str(page_size),
        }
        url = f"{BIRDEYE_BASE}/defi/v2/tokens/top_traders?{urllib.parse.urlencode(params)}"
        data = _http_get_json(url, headers=headers)
        if not data:
            break
        items = (data.get("data") or {}).get("items") or []
        if not items:
            break
        for it in items:
            addr = it.get("address") or it.get("owner") or it.get("wallet")
            if not addr:
                continue
            out.append(TopTrader(
                wallet_address=str(addr),
                token_mint=mint,
                token_symbol=None,
                pnl_usd=_safe_float(it.get("pnl") or it.get("totalPnl") or it.get("total_pnl")),
                volume_usd=_safe_float(it.get("volume") or it.get("volumeUsd")),
                trade_count=_safe_int(it.get("trade") or it.get("trades")),
            ))
            if len(out) >= target_count:
                break
        if len(items) < page_size:
            break
        offset += page_size
        time.sleep(BIRDEYE_SLEEP_SECONDS)
    return out


def get_pool_active_wallets() -> set[str]:
    """Query the wallet_pool table for active-tier addresses.

    Returns an empty set if the framework imports fail (e.g. running outside
    the bot container) — caller treats this as "comparison unavailable".
    """
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


def total_pnl(traders: list[TopTrader]) -> float:
    return sum((t.pnl_usd or 0.0) for t in traders)


def render_markdown(
    from_dt: datetime, to_dt: datetime, args,
    discovered_n: int, mega_pumps: list[MegaPump],
    wallet_tokens: dict[str, list[TopTrader]],
    repeat_winners: dict[str, list[TopTrader]],
    pool_active: set[str],
) -> str:
    lines = []
    lines.append("# Historical Mega-Pump Wallet Analysis\n")
    lines.append(f"- Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- Discovery window: {from_dt.date()} → {to_dt.date()}")
    lines.append(f"- Tokens discovered + history-checked: {discovered_n}")
    lines.append(f"- Mega-pumps (>= {args.min_multiple}x from mint): **{len(mega_pumps)}**")
    lines.append(f"- Unique wallets across all mega-pump top-traders: {len(wallet_tokens)}")
    lines.append(f"- Repeat winners (>= {args.repeat_threshold} different mega-pumps): "
                 f"**{len(repeat_winners)}**")
    lines.append(f"- Our COPY active wallet pool: {len(pool_active)} wallets\n")

    if not mega_pumps:
        lines.append("**No mega-pumps found in this window.** Try a lower --min-multiple "
                     "(e.g. 50 or 25), or a different time window. The pump.fun v3 API "
                     "only exposes the earliest ~2 months (ASC) or the last ~14 days (DESC).\n")
        return "\n".join(lines)

    lines.append("## The mega-pumps that qualified\n")
    lines.append("| # | Symbol | Mint (prefix) | Max multiple | Created |")
    lines.append("|---|---|---|---|---|")
    for i, mp in enumerate(sorted(mega_pumps, key=lambda m: -m.max_multiple)[:30], 1):
        sym = mp.symbol or "?"
        created = mp.created_at.date().isoformat() if mp.created_at else "?"
        lines.append(f"| {i} | {sym} | `{mp.mint[:16]}...` | {mp.max_multiple:.1f}x | {created} |")
    if len(mega_pumps) > 30:
        lines.append(f"| ... | (and {len(mega_pumps)-30} more) | | | |")
    lines.append("")

    if not repeat_winners:
        lines.append("## No repeat winners at this threshold\n")
        lines.append(f"At threshold >= {args.repeat_threshold} mega-pumps, no wallet "
                     f"appeared in multiple winners. That likely means one of:")
        lines.append("- Each pump has its own buyer set (no recurring smart money). "
                     "If true, the 'find quality wallets' thesis is wrong — the edge "
                     "is in real-time detection, not in pre-curated quality wallets.")
        lines.append("- The mega-pump sample is too small for overlap to surface "
                     "(re-run with --min-multiple 50 or 25).")
        lines.append("- Birdeye's 'top traders' endpoint is biased toward late-stage "
                     "high-PnL exiters, not the early accumulators we actually want.\n")
        return "\n".join(lines)

    in_pool = sorted(set(repeat_winners.keys()) & pool_active)
    missing = sorted(set(repeat_winners.keys()) - pool_active)

    lines.append("## Pool gap analysis\n")
    lines.append(f"- Repeat winners ALREADY in our active pool: **{len(in_pool)}**")
    lines.append(f"- Repeat winners MISSING from our pool: **{len(missing)}**\n")

    lines.append("## Top 30 repeat winners by mega-pump appearances\n")
    lines.append("| Rank | Wallet (prefix) | # mega-pumps | Total PnL USD | In our pool? |")
    lines.append("|---|---|---|---|---|")
    top30 = sorted(
        repeat_winners.items(),
        key=lambda kv: (-len(kv[1]), -total_pnl(kv[1])),
    )[:30]
    for i, (wallet, traders) in enumerate(top30, 1):
        marker = "✓" if wallet in pool_active else "✗ ADD"
        lines.append(f"| {i} | `{wallet[:16]}...` | {len(traders)} | "
                     f"${total_pnl(traders):,.0f} | {marker} |")
    lines.append("")

    if missing:
        lines.append("## High-priority pool ADDS (repeat winners NOT yet curated)\n")
        lines.append("These are wallets that appeared in multiple historical mega-pumps "
                     "but are NOT in our active pool. Adding them is the most concrete "
                     "action from this analysis.\n")
        lines.append("| Wallet | # mega-pumps | Total PnL USD |")
        lines.append("|---|---|---|")
        missing_by_count = sorted(
            [(w, repeat_winners[w]) for w in missing],
            key=lambda kv: (-len(kv[1]), -total_pnl(kv[1])),
        )[:20]
        for wallet, traders in missing_by_count:
            lines.append(f"| `{wallet}` | {len(traders)} | ${total_pnl(traders):,.0f} |")
        lines.append("")

    lines.append("## Next steps\n")
    lines.append("1. **Validate the missing wallets via Cielo** — check their 90d PnL/WR "
                 "before promoting. Some Birdeye 'top traders' are MEV bots, not real "
                 "smart money.")
    lines.append("2. **Promote validated wallets to active tier** — extend the existing "
                 "scripts/curate_wallet_pool.py to seed from this script's missing list.")
    lines.append("3. **Re-run with a different window** — ASC vs DESC, or smaller "
                 "--min-multiple to see if the overlap pattern persists at different "
                 "thresholds.\n")
    return "\n".join(lines)


def write_csv(
    path: Path,
    wallet_tokens: dict[str, list[TopTrader]],
    pool_active: set[str],
    repeat_threshold: int,
) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "wallet_address", "n_megapump_tokens", "total_pnl_usd",
            "total_volume_usd", "in_active_pool", "is_repeat_winner",
        ])
        for wallet, traders in sorted(
            wallet_tokens.items(),
            key=lambda kv: (-len(kv[1]), kv[0]),
        ):
            n = len(traders)
            pnl = total_pnl(traders)
            vol = sum((t.volume_usd or 0.0) for t in traders)
            w.writerow([
                wallet, n, f"{pnl:.2f}", f"{vol:.2f}",
                int(wallet in pool_active),
                int(n >= repeat_threshold),
            ])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_str", default="2024-01-25")
    parser.add_argument("--to", dest="to_str", default="2024-02-29")
    parser.add_argument("--sort", choices=("ASC", "DESC"), default="ASC")
    parser.add_argument("--max-tokens", type=int, default=200,
                        help="Max tokens to discover + fetch history for")
    parser.add_argument("--min-multiple", type=float, default=100.0,
                        help="Min max_multiple from mint to qualify as mega-pump")
    parser.add_argument("--traders-per-token", type=int, default=50)
    parser.add_argument("--repeat-threshold", type=int, default=3,
                        help="Wallet must appear in >= this many mega-pumps")
    parser.add_argument("--out-dir", default="/tmp")
    args = parser.parse_args()

    from_dt = datetime.fromisoformat(args.from_str).replace(tzinfo=timezone.utc)
    to_dt = datetime.fromisoformat(args.to_str).replace(tzinfo=timezone.utc)
    if to_dt <= from_dt:
        print("ERROR: --to must be after --from", file=sys.stderr)
        return 2

    # Step 1: discovery
    print(f"\n[step 1/5] Discovering up to {args.max_tokens} pump.fun graduates "
          f"in {from_dt.date()} → {to_dt.date()} (sort={args.sort})...", file=sys.stderr)
    tokens = discover_tokens(
        from_dt, to_dt, args.max_tokens,
        graduated_only=True, sort_order=args.sort,
    )
    if not tokens:
        print("ERROR: no tokens discovered. Re-run pumpfun_discovery_probe to debug.",
              file=sys.stderr)
        return 1
    print(f"  -> {len(tokens)} tokens", file=sys.stderr)

    # Step 2: per-token history → max_multiple
    print(f"\n[step 2/5] Fetching {HISTORY_DAYS}d Birdeye history per token "
          f"(~{len(tokens) * BIRDEYE_SLEEP_SECONDS:.0f}s polite throttle)...",
          file=sys.stderr)
    mega_pumps: list[MegaPump] = []
    n_history_ok = 0
    for i, tok in enumerate(tokens, 1):
        candles = fetch_history_birdeye(
            tok.mint, from_dt=tok.created_at or from_dt, days=HISTORY_DAYS,
        )
        time.sleep(BIRDEYE_SLEEP_SECONDS)
        if not candles:
            continue
        n_history_ok += 1
        feats = compute_features(tok.mint, tok.symbol, candles, graduated=tok.graduated)
        if feats is None:
            continue
        if feats.max_multiple >= args.min_multiple:
            mega_pumps.append(MegaPump(
                mint=tok.mint, symbol=tok.symbol,
                max_multiple=feats.max_multiple,
                final_multiple=feats.final_multiple,
                created_at=tok.created_at,
            ))
            print(f"  [{i:>3}/{len(tokens)}] {tok.symbol or tok.mint[:8]}: "
                  f"max={feats.max_multiple:.1f}x ✓ MEGA-PUMP", file=sys.stderr)
        elif i % 25 == 0:
            print(f"  [{i:>3}/{len(tokens)}] progress: "
                  f"{n_history_ok} with history, {len(mega_pumps)} mega-pumps",
                  file=sys.stderr)
    print(f"\n  -> {len(mega_pumps)} mega-pumps (>= {args.min_multiple}x) from "
          f"{n_history_ok} tokens with usable Birdeye history", file=sys.stderr)

    if not mega_pumps:
        print(f"\n0 mega-pumps. Writing summary report.", file=sys.stderr)
        # Still write a markdown so the run isn't lost
        md = render_markdown(from_dt, to_dt, args, len(tokens), [], {}, {}, set())
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        Path(args.out_dir).mkdir(parents=True, exist_ok=True)
        md_path = Path(args.out_dir) / f"megapump_repeat_winners_{stamp}.md"
        md_path.write_text(md, encoding="utf-8")
        print(f"[done] report: {md_path}", file=sys.stderr)
        return 0

    # Step 3: top traders per mega-pump
    print(f"\n[step 3/5] Fetching top {args.traders_per_token} traders per mega-pump "
          f"({len(mega_pumps) * args.traders_per_token / 10:.0f} Birdeye calls)...",
          file=sys.stderr)
    all_traders: list[TopTrader] = []
    for i, mp in enumerate(mega_pumps, 1):
        traders = fetch_top_traders_birdeye(mp.mint, target_count=args.traders_per_token)
        # Attach symbol for nicer reporting
        for t in traders:
            t.token_symbol = mp.symbol
        all_traders.extend(traders)
        print(f"  [{i:>3}/{len(mega_pumps)}] {mp.symbol or mp.mint[:8]} "
              f"({mp.max_multiple:.0f}x): {len(traders)} traders", file=sys.stderr)

    # Step 4: wallet → tokens map
    print(f"\n[step 4/5] Aggregating wallet → tokens map ({len(all_traders)} trader rows)...",
          file=sys.stderr)
    wallet_tokens: dict[str, list[TopTrader]] = defaultdict(list)
    for t in all_traders:
        wallet_tokens[t.wallet_address].append(t)

    # Step 5: repeat winners + pool gap
    repeat_winners = {
        w: traders for w, traders in wallet_tokens.items()
        if len(traders) >= args.repeat_threshold
    }
    pool_active = get_pool_active_wallets()
    print(f"\n[step 5/5] {len(repeat_winners)} repeat winners "
          f"(>= {args.repeat_threshold} mega-pumps each)", file=sys.stderr)
    if pool_active:
        in_pool = len(set(repeat_winners.keys()) & pool_active)
        missing = len(set(repeat_winners.keys()) - pool_active)
        print(f"  -> {in_pool} already in our active pool", file=sys.stderr)
        print(f"  -> {missing} MISSING from our pool", file=sys.stderr)
    else:
        print(f"  -> pool comparison unavailable (DB not reachable)", file=sys.stderr)

    # Render outputs
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"megapump_repeat_winners_{stamp}.md"
    csv_path = out_dir / f"megapump_per_wallet_{stamp}.csv"
    md = render_markdown(from_dt, to_dt, args, len(tokens), mega_pumps,
                          wallet_tokens, repeat_winners, pool_active)
    md_path.write_text(md, encoding="utf-8")
    write_csv(csv_path, wallet_tokens, pool_active, args.repeat_threshold)

    print(f"\n[done] report: {md_path}", file=sys.stderr)
    print(f"[done] csv:    {csv_path}", file=sys.stderr)
    print(f"\n--- REPORT ---\n", file=sys.stderr)
    print(md, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
