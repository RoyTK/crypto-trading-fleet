"""Whale curation v2 — auto-source candidates from HL trades WS.

Improves on curate_whale_list.py (which required a hand-seeded candidate
list) by sourcing candidates directly from Hyperliquid:

Phase 1 — collect candidates:
  - Subscribe to HL trades WS for top-N vol assets
  - For SAMPLE_DURATION_SECONDS (default 1800 = 30 min), accumulate
    per-user total notional traded across those markets
  - Take the top CANDIDATE_POOL_SIZE addresses by total notional
  - Optionally seed in addresses from an existing whale_list.json

Phase 2 — filter by historical stats + current position:
  For each candidate:
    - Fetch last 180-day fills via Info.user_fills_by_time
    - Compute closed_positions, win_rate, cumulative_notional, avg_hold
    - Fetch current user_state, find largest open position size
  Filters:
    - win_rate >= MIN_WIN_RATE (0.60)
    - closed_positions >= MIN_TRADES (20)
    - cumulative_notional >= MIN_CUMULATIVE_NOTIONAL_USD ($5M)
    - max_current_open_position_usd >= MIN_CURRENT_POSITION_USD ($500k)
      ^ NEW filter — fixes the "all whales currently flat" failure mode

Phase 3 — output:
  Write top WHALE_TARGET_COUNT to bots/structure/whale_list.json with
  augmented schema (adds current_max_position_usd field).

Usage:
    docker compose exec -T framework python -m scripts.curate_whales_v2 \\
        --sample-seconds 1800 \\
        --candidate-pool 300 \\
        --target 50 \\
        --seed-existing
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hyperliquid.info import Info

from bots.structure.config import get_structure_settings
from bots.structure.venue import HyperliquidVenue


SIX_MONTHS_MS = 180 * 24 * 60 * 60 * 1000
WL_PATH = Path("bots/structure/whale_list.json")
CACHE_DIR = Path("bots/structure/whale_ws_cache")


@dataclass
class WhaleStats:
    address: str
    tag: str
    closed_positions: int
    win_rate: float
    cumulative_notional_usd: float
    avg_hold_minutes: float
    current_max_position_usd: float
    score: float                # win_rate * cumulative_notional


# ---------------------------------------------------------------------------
# Phase 1: candidate collection
# ---------------------------------------------------------------------------

def collect_candidates_from_ws(
    api_url: str,
    top_n_assets: int,
    sample_seconds: int,
) -> dict[str, float]:
    """Sample HL trades WS; return {address: total_notional_usd_traded}."""
    venue = HyperliquidVenue()
    asset_ctxs = venue.asset_contexts()
    top_assets = sorted(asset_ctxs, key=lambda c: c.day_volume_usd, reverse=True)[:top_n_assets]
    coins = [c.asset for c in top_assets]
    print(f"[ws] sampling {len(coins)} top-vol assets: {coins}", file=sys.stderr)

    # One Info instance with skip_ws=False; subscribe to each coin's trades
    info = Info(api_url, skip_ws=False)
    user_volume: dict[str, float] = defaultdict(float)
    event_count = [0]  # mutable in closure

    def make_cb(coin: str):
        def cb(msg: dict[str, Any]) -> None:
            try:
                data = msg.get("data") or []
                if not isinstance(data, list):
                    return
                for trade in data:
                    sz = float(trade.get("sz", 0) or 0)
                    px = float(trade.get("px", 0) or 0)
                    notional = sz * px
                    if notional <= 0:
                        continue
                    users = trade.get("users") or []
                    # Each trade has [taker, maker]; both contribute volume
                    for u in users:
                        if isinstance(u, str) and u.startswith("0x"):
                            user_volume[u.lower()] += notional
                    event_count[0] += 1
            except Exception:
                pass
        return cb

    for coin in coins:
        info.subscribe({"type": "trades", "coin": coin}, make_cb(coin))

    print(f"[ws] subscribed; sampling for {sample_seconds}s ...", file=sys.stderr)
    deadline = time.time() + sample_seconds
    while time.time() < deadline:
        time.sleep(10)
        elapsed = sample_seconds - int(deadline - time.time())
        print(f"  t={elapsed}s | events={event_count[0]} | unique_users={len(user_volume)}", file=sys.stderr)

    print(f"[ws] done. {event_count[0]} events, {len(user_volume)} unique addresses", file=sys.stderr)
    return dict(user_volume)


# ---------------------------------------------------------------------------
# Phase 1.5: counterparty expansion
# ---------------------------------------------------------------------------

def expand_via_counterparties(
    venue: HyperliquidVenue,
    seed_addresses: list[str],
    max_fills_per_seed: int = 100,
) -> set[str]:
    """For each seed, fetch recent fills and extract counterparty addresses.

    Counterparties of $500k+ position holders are by definition large traders.
    This catches periodic whales who didn't trade during the WS sample window
    but ARE on the other side of trades made by whales who did.
    """
    out: set[str] = set()
    since_ms = int(time.time() * 1000) - 7 * 24 * 60 * 60 * 1000  # last 7 days
    for i, seed in enumerate(seed_addresses, start=1):
        try:
            fn = getattr(venue.info, "user_fills_by_time", None)
            if fn is None:
                fn = getattr(venue.info, "user_fills")
                fills = fn(seed)
            else:
                fills = fn(seed, since_ms)
        except Exception:
            continue
        if not fills:
            continue
        # Walk fills in reverse-chronological, take up to max_fills_per_seed
        for f in fills[:max_fills_per_seed]:
            users = f.get("users") or []
            for u in users:
                if isinstance(u, str) and u.startswith("0x"):
                    addr = u.lower()
                    if addr != seed.lower():
                        out.add(addr)
        if i % 5 == 0:
            print(f"  [counterparty] {i}/{len(seed_addresses)} seeds processed; "
                  f"unique counterparties so far: {len(out)}", file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# Phase 2: stats + current position filter (lifted from curate_whale_list)
# ---------------------------------------------------------------------------

def _user_fills(venue: HyperliquidVenue, address: str, since_ms: int) -> list[dict[str, Any]]:
    try:
        info = venue.info
        fn = getattr(info, "user_fills_by_time", None) or getattr(info, "user_fills")
        if fn is None:
            return []
        return fn(address, since_ms) if "by_time" in fn.__name__ else fn(address)
    except Exception:
        return []


def _max_current_position_usd(venue: HyperliquidVenue, address: str) -> float:
    """Return the USD notional of the largest currently-open position."""
    try:
        state = venue.info.user_state(address)
    except Exception:
        return 0.0
    max_notional = 0.0
    for ap in state.get("assetPositions", []) or []:
        pos = ap.get("position", {}) or {}
        try:
            sz_native = abs(float(pos.get("szi", 0) or 0))
            entry_px = float(pos.get("entryPx", 0) or 0)
        except (TypeError, ValueError):
            continue
        notional = sz_native * entry_px
        if notional > max_notional:
            max_notional = notional
    return max_notional


def _stats_from_fills(address: str, fills: list[dict[str, Any]], current_max_pos: float) -> WhaleStats | None:
    """Aggregate fills into closed-position stats."""
    if not fills:
        return None
    fills = sorted(fills, key=lambda f: f.get("time", 0))
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
        crossed_zero = (
            (prior_size > 0 and new_size <= 0)
            or (prior_size < 0 and new_size >= 0)
        )
        if crossed_zero and prior_size != 0:
            close_qty = min(abs(prior_size), abs(signed_sz))
            avg_entry = (st["cost"] / abs(prior_size)) if prior_size else 0.0
            pnl_per_unit = (px - avg_entry) if prior_size > 0 else (avg_entry - px)
            pnl = pnl_per_unit * close_qty
            closed_positions += 1
            if pnl > 0:
                wins += 1
            if coin in open_started_at:
                hold_durations_ms.append(max(0, ts - open_started_at[coin]))
            if abs(new_size) < 1e-9:
                st["size"] = 0.0
                st["cost"] = 0.0
                open_started_at.pop(coin, None)
            else:
                st["size"] = new_size
                st["cost"] = abs(new_size) * px
                open_started_at[coin] = ts
        else:
            if (prior_size >= 0 and signed_sz > 0) or (prior_size <= 0 and signed_sz < 0):
                st["cost"] = st["cost"] + sz * px
                st["size"] = new_size
                if prior_size == 0:
                    open_started_at[coin] = ts
            else:
                st["size"] = new_size
                st["cost"] = abs(new_size) * (st["cost"] / abs(prior_size)) if prior_size else 0.0

    if closed_positions == 0:
        return None
    win_rate = wins / closed_positions
    avg_hold_minutes = (
        sum(hold_durations_ms) / len(hold_durations_ms) / 60_000.0
        if hold_durations_ms else 0.0
    )
    score = cumulative_notional * win_rate
    return WhaleStats(
        address=address,
        tag="",
        closed_positions=closed_positions,
        win_rate=win_rate,
        cumulative_notional_usd=cumulative_notional,
        avg_hold_minutes=avg_hold_minutes,
        current_max_position_usd=current_max_pos,
        score=score,
    )


# ---------------------------------------------------------------------------
# Phase 3: filter + output
# ---------------------------------------------------------------------------

def filter_and_rank(
    candidates: list[str],
    venue: HyperliquidVenue,
    min_win_rate: float,
    min_trades: int,
    min_cumulative_notional_usd: float,
    min_current_position_usd: float,
) -> list[WhaleStats]:
    since_ms = int(time.time() * 1000) - SIX_MONTHS_MS
    qualified: list[WhaleStats] = []
    skipped_no_pos = 0
    skipped_pos_too_small = 0
    skipped_no_fills = 0
    skipped_low_wr = 0
    skipped_low_trades = 0
    skipped_low_vol = 0

    for i, addr in enumerate(candidates, start=1):
        if i % 25 == 0:
            print(f"  [{i}/{len(candidates)}] qualified={len(qualified)} | "
                  f"skip pos<min={skipped_pos_too_small} no_pos={skipped_no_pos} "
                  f"no_fills={skipped_no_fills} wr<min={skipped_low_wr} "
                  f"trades<min={skipped_low_trades} vol<min={skipped_low_vol}",
                  file=sys.stderr)
        current_max_pos = _max_current_position_usd(venue, addr)
        if current_max_pos < min_current_position_usd:
            if current_max_pos == 0.0:
                skipped_no_pos += 1
            else:
                skipped_pos_too_small += 1
            continue
        fills = _user_fills(venue, addr, since_ms)
        stats = _stats_from_fills(addr, fills, current_max_pos)
        if stats is None:
            skipped_no_fills += 1
            continue
        if stats.win_rate < min_win_rate:
            skipped_low_wr += 1
            continue
        if stats.closed_positions < min_trades:
            skipped_low_trades += 1
            continue
        if stats.cumulative_notional_usd < min_cumulative_notional_usd:
            skipped_low_vol += 1
            continue
        qualified.append(stats)
        print(
            f"  OK {addr[:10]}.. trades={stats.closed_positions} wr={stats.win_rate:.2f} "
            f"vol=${stats.cumulative_notional_usd:,.0f} cur_pos=${stats.current_max_position_usd:,.0f}",
            file=sys.stderr,
        )

    print(f"\n[final tally] qualified={len(qualified)} | "
          f"skip pos<min={skipped_pos_too_small} no_pos={skipped_no_pos} "
          f"no_fills={skipped_no_fills} wr<min={skipped_low_wr} "
          f"trades<min={skipped_low_trades} vol<min={skipped_low_vol}",
          file=sys.stderr)
    qualified.sort(key=lambda s: s.score, reverse=True)
    return qualified


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-seconds", type=int, default=1800,
                        help="WS sampling duration (default 30 min). Set to 0 to skip Phase 1 and load from cache.")
    parser.add_argument("--top-n-assets", type=int, default=15,
                        help="Sample trades on top-N volume assets (default 15)")
    parser.add_argument("--candidate-pool", type=int, default=300,
                        help="How many top-volume addresses to evaluate (default 300)")
    parser.add_argument("--target", type=int, default=50,
                        help="Target whale list size (default 50)")
    parser.add_argument("--min-win-rate", type=float, default=0.60)
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument("--min-cumulative-notional", type=float, default=5_000_000.0)
    parser.add_argument("--min-current-position-usd", type=float, default=250_000.0,
                        help="Whale must currently hold a position >= this size (default 250k)")
    parser.add_argument("--seed-existing", action="store_true",
                        help="Include addresses from existing whale_list.json")
    parser.add_argument("--expand-counterparties", action="store_true",
                        help="After WS sampling, expand pool with counterparties of top-K candidates")
    parser.add_argument("--counterparty-seeds", type=int, default=30,
                        help="Use top-N WS candidates as counterparty seeds (default 30)")
    parser.add_argument("--counterparty-fills-per-seed", type=int, default=100,
                        help="Pull up to N recent fills per seed (default 100)")
    parser.add_argument("--accumulate-from-cache", action="store_true",
                        help="Load + sum all *.json files in whale_ws_cache/ into the candidate pool")
    parser.add_argument("--save-cache", action="store_true", default=True,
                        help="Persist this run's WS sample to whale_ws_cache/ (default true)")
    parser.add_argument("--out", default=str(WL_PATH))
    parser.add_argument("--dry-run", action="store_true",
                        help="Print summary; do not write the file")
    args = parser.parse_args()

    settings = get_structure_settings()

    user_volume: dict[str, float] = {}

    # Optionally load + sum prior cached samples
    if args.accumulate_from_cache and CACHE_DIR.exists():
        cache_files = sorted(CACHE_DIR.glob("*.json"))
        for cf in cache_files:
            try:
                cached = json.loads(cf.read_text())
                for addr, vol in cached.get("user_volume", {}).items():
                    user_volume[addr.lower()] = user_volume.get(addr.lower(), 0.0) + float(vol)
                print(f"[cache] loaded {cf.name}: +{len(cached.get('user_volume', {}))} addresses",
                      file=sys.stderr)
            except Exception as e:
                print(f"[cache] failed to load {cf}: {e}", file=sys.stderr)
        print(f"[cache] total accumulated: {len(user_volume)} unique addresses", file=sys.stderr)

    # Run a fresh WS sample unless explicitly skipped (sample_seconds=0)
    if args.sample_seconds > 0:
        print("\n=== Phase 1: collecting candidates from trades WS ===", file=sys.stderr)
        fresh_volume = collect_candidates_from_ws(
            api_url=settings.hyperliquid_api_url,
            top_n_assets=args.top_n_assets,
            sample_seconds=args.sample_seconds,
        )
        # Save THIS sample as its own cache file
        if args.save_cache:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            cache_path = CACHE_DIR / f"sample_{ts}.json"
            cache_path.write_text(json.dumps({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "sample_seconds": args.sample_seconds,
                "top_n_assets": args.top_n_assets,
                "user_volume": fresh_volume,
            }))
            print(f"[cache] saved this sample to {cache_path}", file=sys.stderr)
        # Merge into accumulated
        for addr, vol in fresh_volume.items():
            user_volume[addr.lower()] = user_volume.get(addr.lower(), 0.0) + vol

    if not user_volume:
        print("[error] no candidates collected. Either run a sample (--sample-seconds > 0) "
              "or accumulate from cache (--accumulate-from-cache).", file=sys.stderr)
        sys.exit(2)

    sorted_by_volume = sorted(user_volume.items(), key=lambda kv: kv[1], reverse=True)
    candidates = [addr for addr, _ in sorted_by_volume[: args.candidate_pool]]
    candidate_set = {a.lower() for a in candidates}

    if args.seed_existing and WL_PATH.exists():
        try:
            existing = json.loads(WL_PATH.read_text())
            n_added = 0
            for w in existing.get("whales", []):
                addr = w.get("address", "").lower()
                if addr and addr not in candidate_set:
                    candidates.append(addr)
                    candidate_set.add(addr)
                    n_added += 1
            print(f"[seed] added {n_added} existing whales to pool", file=sys.stderr)
        except Exception:
            print("[seed] failed to load existing whale_list.json, skipping", file=sys.stderr)

    if args.expand_counterparties:
        venue_for_expand = HyperliquidVenue()
        seeds = [a for a, _ in sorted_by_volume[: args.counterparty_seeds]]
        print(f"\n=== Phase 1.5: counterparty expansion from top-{len(seeds)} WS candidates ===",
              file=sys.stderr)
        counterparties = expand_via_counterparties(
            venue_for_expand, seeds,
            max_fills_per_seed=args.counterparty_fills_per_seed,
        )
        n_added = 0
        for addr in counterparties:
            if addr not in candidate_set:
                candidates.append(addr)
                candidate_set.add(addr)
                n_added += 1
        print(f"[counterparty] added {n_added} new addresses (from {len(counterparties)} extracted)",
              file=sys.stderr)

    print(f"[total] evaluating {len(candidates)} unique candidates", file=sys.stderr)

    print(f"\n=== Phase 2: filtering (win_rate>={args.min_win_rate}, "
          f"trades>={args.min_trades}, vol>=${args.min_cumulative_notional:,.0f}, "
          f"current_pos>=${args.min_current_position_usd:,.0f}) ===", file=sys.stderr)
    venue = HyperliquidVenue()
    qualified = filter_and_rank(
        candidates, venue,
        min_win_rate=args.min_win_rate,
        min_trades=args.min_trades,
        min_cumulative_notional_usd=args.min_cumulative_notional,
        min_current_position_usd=args.min_current_position_usd,
    )
    top = qualified[: args.target]

    print(f"\n=== Phase 3: writing {len(top)} whales (qualified={len(qualified)}) ===", file=sys.stderr)

    out_obj = {
        "version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": (
            f"curate_whales_v2.py: WS sample {args.sample_seconds}s on top-{args.top_n_assets} "
            f"assets, evaluated {len(candidates)} candidates, qualified {len(qualified)}, kept top {len(top)}"
        ),
        "filters": {
            "min_win_rate": args.min_win_rate,
            "min_trades": args.min_trades,
            "min_cumulative_notional_usd": args.min_cumulative_notional,
            "min_current_position_usd": args.min_current_position_usd,
        },
        "whales": [
            {
                "address": s.address,
                "tag": s.tag,
                "historical_win_rate": round(s.win_rate, 4),
                "closed_positions_6mo": s.closed_positions,
                "cumulative_notional_usd": round(s.cumulative_notional_usd, 2),
                "avg_hold_minutes": round(s.avg_hold_minutes, 1),
                "current_max_position_usd": round(s.current_max_position_usd, 2),
            }
            for s in top
        ],
    }

    if args.dry_run:
        print(json.dumps(out_obj, indent=2))
        return

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_obj, indent=2))
    print(f"[done] wrote {len(top)} whales to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
