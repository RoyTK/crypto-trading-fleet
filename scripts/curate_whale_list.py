"""One-shot whale list curation.

Reads a candidate-address list (provided as a JSON file via --candidates), pulls
6 months of fills from Hyperliquid for each, computes win rate + total notional,
and writes the top filtered whales to bots/structure/whale_list.json.

Usage:
    python -m scripts.curate_whale_list --candidates seed_addresses.json --out bots/structure/whale_list.json

Filters (locked v0):
- Win rate >= 60%
- >=20 closed positions in last 6 months
- Cumulative notional >=$5M
- Output sorted by (volume * win_rate); take top 50

The candidate-address seed list is sourced from public Hyperliquid
leaderboards (Hypurrscan, Hyperdash, etc.). For Build A we ship with an
empty whale_list.json — the detector handles that. Run this script during
shakedown prep with a fresh candidate list.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bots.structure.venue import HyperliquidVenue


SIX_MONTHS_MS = 180 * 24 * 60 * 60 * 1000


@dataclass
class WhaleStats:
    address: str
    tag: str
    closed_positions: int
    win_rate: float
    cumulative_notional_usd: float
    avg_hold_minutes: float
    score: float                 # volume * win_rate; used for ranking


def _user_fills(venue: HyperliquidVenue, address: str, since_ms: int) -> list[dict[str, Any]]:
    """Pull fills via Info.user_fills_by_time. Limited to ~2000 most recent."""
    try:
        info = venue.info
        fn = getattr(info, "user_fills_by_time", None) or getattr(info, "user_fills")
        if fn is None:
            return []
        return fn(address, since_ms) if "by_time" in fn.__name__ else fn(address)
    except Exception:
        return []


def _stats_from_fills(address: str, tag: str, fills: list[dict[str, Any]]) -> WhaleStats | None:
    """Aggregate fills into closed-position stats. Approximate — fills don't
    explicitly mark "close" events, so we infer by net-position-zero crossings.
    """
    if not fills:
        return None

    # Sort by time ascending
    fills = sorted(fills, key=lambda f: f.get("time", 0))

    # Per-asset running position; count closed positions and PnL
    pos_by_asset: dict[str, dict[str, float]] = {}
    closed_positions = 0
    wins = 0
    cumulative_notional = 0.0
    open_started_at: dict[str, int] = {}
    hold_durations_ms: list[int] = []

    for f in fills:
        coin = f.get("coin", "")
        if not coin:
            continue
        sz = float(f.get("sz", 0) or 0)
        px = float(f.get("px", 0) or 0)
        side = f.get("side", "")
        ts = int(f.get("time", 0) or 0)
        signed_sz = sz if side in ("B", "buy") else -sz
        notional = abs(sz * px)
        cumulative_notional += notional

        st = pos_by_asset.setdefault(coin, {"size": 0.0, "cost": 0.0})
        prior_size = st["size"]
        new_size = prior_size + signed_sz

        # Close detection: sign change OR exact zero from non-zero
        crossed_zero = (
            (prior_size > 0 and new_size <= 0)
            or (prior_size < 0 and new_size >= 0)
        )
        if crossed_zero and prior_size != 0:
            # Approximate PnL on the closing portion
            close_qty = min(abs(prior_size), abs(signed_sz))
            avg_entry = (st["cost"] / abs(prior_size)) if prior_size else 0.0
            pnl_per_unit = (px - avg_entry) if prior_size > 0 else (avg_entry - px)
            pnl = pnl_per_unit * close_qty
            closed_positions += 1
            if pnl > 0:
                wins += 1
            if coin in open_started_at:
                hold_durations_ms.append(max(0, ts - open_started_at[coin]))
            # Reset cost basis if fully closed; if reversed, init with remainder
            if abs(new_size) < 1e-9:
                st["size"] = 0.0
                st["cost"] = 0.0
                open_started_at.pop(coin, None)
            else:
                # Started a new opposite position
                st["size"] = new_size
                st["cost"] = abs(new_size) * px
                open_started_at[coin] = ts
        else:
            # Adding to or reducing existing
            if (prior_size >= 0 and signed_sz > 0) or (prior_size <= 0 and signed_sz < 0):
                # Adding
                st["cost"] = st["cost"] + sz * px
                st["size"] = new_size
                if prior_size == 0:
                    open_started_at[coin] = ts
            else:
                # Reducing without crossing
                st["size"] = new_size
                st["cost"] = abs(new_size) * (st["cost"] / abs(prior_size)) if prior_size else 0.0

    if closed_positions == 0:
        return None

    win_rate = wins / closed_positions
    avg_hold_minutes = (sum(hold_durations_ms) / len(hold_durations_ms) / 60_000.0) if hold_durations_ms else 0.0
    score = cumulative_notional * win_rate
    return WhaleStats(
        address=address,
        tag=tag,
        closed_positions=closed_positions,
        win_rate=win_rate,
        cumulative_notional_usd=cumulative_notional,
        avg_hold_minutes=avg_hold_minutes,
        score=score,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", required=True, help="JSON list of {address, tag} candidates")
    parser.add_argument("--out", default="bots/structure/whale_list.json")
    parser.add_argument("--top", type=int, default=50)
    parser.add_argument("--min-win-rate", type=float, default=0.60)
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument("--min-notional-usd", type=float, default=5_000_000.0)
    args = parser.parse_args()

    with open(args.candidates, "r", encoding="utf-8") as f:
        candidates = json.load(f)

    if isinstance(candidates, dict) and "candidates" in candidates:
        candidates = candidates["candidates"]

    venue = HyperliquidVenue()
    since_ms = int(time.time() * 1000) - SIX_MONTHS_MS

    qualified: list[WhaleStats] = []
    for entry in candidates:
        if isinstance(entry, str):
            entry = {"address": entry, "tag": ""}
        addr = entry["address"]
        tag = entry.get("tag", "")
        print(f"[fetch] {addr} ({tag})", file=sys.stderr)
        fills = _user_fills(venue, addr, since_ms)
        stats = _stats_from_fills(addr, tag, fills)
        if stats is None:
            continue
        if (
            stats.win_rate >= args.min_win_rate
            and stats.closed_positions >= args.min_trades
            and stats.cumulative_notional_usd >= args.min_notional_usd
        ):
            qualified.append(stats)
            print(
                f"  OK: trades={stats.closed_positions} wr={stats.win_rate:.2f} "
                f"vol=${stats.cumulative_notional_usd:,.0f}",
                file=sys.stderr,
            )

    qualified.sort(key=lambda s: s.score, reverse=True)
    top = qualified[: args.top]

    out_obj = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": f"curate_whale_list.py from {args.candidates}",
        "notes": (
            f"Filters: win_rate>={args.min_win_rate}, trades>={args.min_trades}, "
            f"notional>=${args.min_notional_usd:,.0f}. "
            f"Examined {len(candidates)} candidates; qualified {len(qualified)}; kept top {len(top)}."
        ),
        "whales": [
            {
                "address": s.address,
                "tag": s.tag,
                "historical_win_rate": round(s.win_rate, 4),
                "closed_positions_6mo": s.closed_positions,
                "cumulative_notional_usd": round(s.cumulative_notional_usd, 2),
                "avg_hold_minutes": round(s.avg_hold_minutes, 1),
            }
            for s in top
        ],
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out_obj, f, indent=2)
    print(f"[done] wrote {len(top)} whales to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
