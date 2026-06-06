"""Helius entry-timing probe: when did this wallet first buy this token?

Answers the question "is wallet X currently accumulating, distributing, or
bag-holding token Y" by pulling its Helius enhanced-tx history filtered to
that specific token mint and classifying transfers IN (buy) vs OUT (sell).

Reports per (wallet, mint) pair:
- First inbound transfer timestamp (= initial entry)
- Total inbound amount, total outbound amount, net position change
- Last activity timestamp (was there a buy/sell in last 7/30 days?)
- Per-month breakdown of buys vs sells for accumulation/distribution call

Usage:
  docker compose exec bot_copy python -m scripts.wallet_entry_timing_probe \\
    --wallet 9ZPsRWGkukYeWg2Z7eZ8NaTBZ1DSuBUVzLcGQWZgE4Y4 \\
    --mint PYTHIA_MINT_ADDRESS

  # Multi-pair: pass --pairs-csv with `wallet,mint` rows
  docker compose exec bot_copy python -m scripts.wallet_entry_timing_probe \\
    --pairs-csv /tmp/wallet_token_pairs.csv

Cost: ~50-200 Helius credits per (wallet, mint) pair depending on history
depth. For 5 pairs that's <1k credits = under 0.01% of monthly 10M quota.

Output:
- Markdown report to /tmp/entry_timing_<ts>.md
- One row per pair printed to stdout
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


HELIUS_BASE = "https://api.helius.xyz"
USER_AGENT = "crypto-fleet-research/1.0"

# Pump.fun and PumpSwap mint addresses we care about most often
KNOWN_MINTS = {
    "PYTHIA": "9wq4iJxBoBfHZ3JaYzQ9YHrCqcQHCMcS2pythiapump",  # placeholder; Roy verifies
    "ARC": "AGFEad2et2ZJif9jaGpdMixQqvW5i81aBdvKe7PHNfz3arc",  # placeholder
    "ALCH": "HJUfqXoYjC653f2p33i84zdCC3jc4EuVnbruSe5kpump",  # placeholder; verify
}

# Common known patterns to flag fast
EXCHANGE_WALLETS = {
    # Helius's known-entity tagging would be better; this is a quick filter
    # for obvious CEX wallets so we don't analyze them
}


@dataclass
class Transfer:
    ts: datetime
    direction: str  # 'in' (buy/receive) or 'out' (sell/send)
    amount: float
    sig: str
    counterparty: Optional[str] = None


@dataclass
class TokenHistory:
    wallet: str
    mint: str
    transfers: list[Transfer] = field(default_factory=list)
    n_pages_scanned: int = 0
    api_errors: int = 0

    @property
    def n_in(self) -> int:
        return sum(1 for t in self.transfers if t.direction == "in")

    @property
    def n_out(self) -> int:
        return sum(1 for t in self.transfers if t.direction == "out")

    @property
    def total_in(self) -> float:
        return sum(t.amount for t in self.transfers if t.direction == "in")

    @property
    def total_out(self) -> float:
        return sum(t.amount for t in self.transfers if t.direction == "out")

    @property
    def net_position(self) -> float:
        return self.total_in - self.total_out

    @property
    def first_buy(self) -> Optional[datetime]:
        ins = [t.ts for t in self.transfers if t.direction == "in"]
        return min(ins) if ins else None

    @property
    def last_activity(self) -> Optional[datetime]:
        return max((t.ts for t in self.transfers), default=None)

    def activity_recent(self, days: int) -> tuple[int, int]:
        """Return (n_buys, n_sells) in the last `days` days."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        buys = sum(1 for t in self.transfers if t.direction == "in" and t.ts >= cutoff)
        sells = sum(1 for t in self.transfers if t.direction == "out" and t.ts >= cutoff)
        return buys, sells

    def monthly_breakdown(self) -> dict:
        """Return {YYYY-MM: (n_buys, n_sells, net_volume)}."""
        out: dict[str, list[float]] = defaultdict(lambda: [0, 0, 0.0])
        for t in self.transfers:
            key = t.ts.strftime("%Y-%m")
            if t.direction == "in":
                out[key][0] += 1
                out[key][2] += t.amount
            else:
                out[key][1] += 1
                out[key][2] -= t.amount
        return {k: tuple(v) for k, v in sorted(out.items())}

    def verdict(self) -> str:
        """One-word classification: ACCUMULATING / DISTRIBUTING / HOLDING / SOLD_OUT / NONE."""
        if not self.transfers:
            return "NONE"
        if self.net_position <= 0:
            return "SOLD_OUT"
        recent_buys, recent_sells = self.activity_recent(30)
        if recent_buys > recent_sells and recent_buys > 0:
            return "ACCUMULATING"
        if recent_sells > recent_buys and recent_sells > 0:
            return "DISTRIBUTING"
        if recent_buys == 0 and recent_sells == 0:
            # Has position but no recent activity
            return "HOLDING_INACTIVE"
        return "HOLDING_LIGHT"


def fetch_address_transactions_page(
    address: str, api_key: str, before: Optional[str] = None, limit: int = 100
) -> Optional[list[dict]]:
    params = {"api-key": api_key, "limit": str(limit)}
    if before:
        params["before"] = before
    url = f"{HELIUS_BASE}/v0/addresses/{address}/transactions?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = e.read()[:200].decode("utf-8", errors="replace")
        except Exception:
            body = ""
        print(f"  HTTPError {e.code}: {body}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  {type(e).__name__}: {e}", file=sys.stderr)
        return None


def collect_token_history(
    wallet: str, mint: str, api_key: str,
    max_pages: int = 30, throttle_seconds: float = 0.3,
) -> TokenHistory:
    """Paginate the wallet's tx history, accumulating any tx involving `mint`.

    Helius enhanced txs include `tokenTransfers` arrays. We filter to ones
    where `mint` matches AND wallet is fromUserAccount (out) or
    toUserAccount (in).

    Stops when:
    - max_pages reached
    - API returns empty page
    - API returns error
    """
    history = TokenHistory(wallet=wallet, mint=mint)
    before: Optional[str] = None
    for page in range(max_pages):
        txs = fetch_address_transactions_page(wallet, api_key, before=before)
        history.n_pages_scanned = page + 1
        if txs is None:
            history.api_errors += 1
            break
        if not txs:
            break
        for tx in txs:
            ts_unix = tx.get("timestamp") or 0
            ts = datetime.fromtimestamp(int(ts_unix), tz=timezone.utc)
            sig = tx.get("signature", "")
            for tt in (tx.get("tokenTransfers") or []):
                if tt.get("mint") != mint:
                    continue
                from_addr = tt.get("fromUserAccount") or ""
                to_addr = tt.get("toUserAccount") or ""
                amount = float(tt.get("tokenAmount") or 0)
                if amount <= 0:
                    continue
                if to_addr == wallet:
                    history.transfers.append(Transfer(
                        ts=ts, direction="in", amount=amount,
                        sig=sig, counterparty=from_addr or None,
                    ))
                elif from_addr == wallet:
                    history.transfers.append(Transfer(
                        ts=ts, direction="out", amount=amount,
                        sig=sig, counterparty=to_addr or None,
                    ))
        # Cursor for next page
        if txs:
            before = txs[-1].get("signature")
        if not before:
            break
        time.sleep(throttle_seconds)
    return history


def render_markdown(histories: list[TokenHistory]) -> str:
    lines: list[str] = []
    lines.append("# Wallet → Token Entry Timing Probe\n")
    lines.append(f"- Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- Pairs analyzed: {len(histories)}\n")

    lines.append("## Headline verdicts\n")
    lines.append("| Wallet (prefix) | Mint (prefix) | First buy | Last activity "
                 "| Buys 7d / 30d | Sells 7d / 30d | Net pos | Verdict |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for h in histories:
        wallet_s = f"`{h.wallet[:14]}...`"
        mint_s = f"`{h.mint[:14]}...`"
        first_s = h.first_buy.date().isoformat() if h.first_buy else "—"
        last_s = h.last_activity.date().isoformat() if h.last_activity else "—"
        b7, s7 = h.activity_recent(7)
        b30, s30 = h.activity_recent(30)
        net_s = f"{h.net_position:,.2f}"
        lines.append(f"| {wallet_s} | {mint_s} | {first_s} | {last_s} | "
                     f"{b7} / {b30} | {s7} / {s30} | {net_s} | **{h.verdict()}** |")
    lines.append("")

    for h in histories:
        if not h.transfers:
            lines.append(f"### `{h.wallet[:16]}...` × `{h.mint[:16]}...`: no transfers found")
            continue
        lines.append(f"### `{h.wallet[:16]}...` × `{h.mint[:16]}...`")
        lines.append(f"- N transfers: {len(h.transfers)} ({h.n_in} in / {h.n_out} out)")
        lines.append(f"- Total in: {h.total_in:,.2f}  |  Total out: {h.total_out:,.2f}")
        lines.append(f"- Net position: **{h.net_position:,.2f}**")
        lines.append(f"- First buy: {h.first_buy.isoformat() if h.first_buy else '—'}")
        lines.append(f"- Last activity: {h.last_activity.isoformat() if h.last_activity else '—'}")
        lines.append(f"- Pages scanned: {h.n_pages_scanned}")
        lines.append("")
        lines.append("**Monthly breakdown** (buys / sells / net volume):")
        lines.append("| Month | Buys | Sells | Net volume |")
        lines.append("|---|---|---|---|")
        for month, (b, s, net) in h.monthly_breakdown().items():
            lines.append(f"| {month} | {b} | {s} | {net:,.2f} |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wallet", help="Single wallet address (use with --mint)")
    parser.add_argument("--mint", help="Single token mint (use with --wallet)")
    parser.add_argument("--pairs-csv", help="CSV with wallet,mint columns")
    parser.add_argument("--max-pages", type=int, default=30,
                        help="Max Helius pagination depth per wallet (100 txs/page)")
    parser.add_argument("--out-dir", default="/tmp")
    args = parser.parse_args()

    api_key = os.environ.get("HELIUS_API_KEY")
    if not api_key:
        print("ERROR: HELIUS_API_KEY required", file=sys.stderr)
        return 1

    # Build pair list
    pairs: list[tuple[str, str]] = []
    if args.wallet and args.mint:
        pairs.append((args.wallet, args.mint))
    if args.pairs_csv:
        with open(args.pairs_csv) as f:
            reader = csv.reader(f)
            for row in reader:
                if not row or row[0].startswith("#") or len(row) < 2:
                    continue
                pairs.append((row[0].strip(), row[1].strip()))
    if not pairs:
        print("ERROR: provide --wallet+--mint or --pairs-csv", file=sys.stderr)
        return 2

    histories: list[TokenHistory] = []
    for i, (wallet, mint) in enumerate(pairs, 1):
        print(f"[{i:>3}/{len(pairs)}] probing {wallet[:16]} for {mint[:16]}...",
              file=sys.stderr)
        h = collect_token_history(wallet, mint, api_key,
                                    max_pages=args.max_pages)
        print(f"    -> {len(h.transfers)} transfers ({h.n_in} in / {h.n_out} out), "
              f"verdict: {h.verdict()}", file=sys.stderr)
        histories.append(h)

    md = render_markdown(histories)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"entry_timing_{stamp}.md"
    md_path.write_text(md, encoding="utf-8")

    print(f"\n[done] report: {md_path}", file=sys.stderr)
    print(f"\n--- REPORT ---\n", file=sys.stderr)
    print(md, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
