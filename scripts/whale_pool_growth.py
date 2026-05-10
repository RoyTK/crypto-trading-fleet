"""Additive whale-pool growth (5-hour cron).

Differs from curate_whales_v2.py in TWO ways:
1. ADDITIVE — never overwrites existing whale_list.json. New qualifying
   whales are appended; existing whales stay (even if they no longer pass
   the filter — staleness is handled by the periodic replacement cycle).
2. CAPPED — stops adding once pool reaches WHALE_POOL_TARGET_MIN (default 50).

Intended cron entry on Hetzner (fleet user):
    0 */5 * * * cd /home/fleet/crypto-fleet && /usr/bin/docker compose exec -T \\
      framework python -m scripts.whale_pool_growth \\
      >> /home/fleet/logs/whale_pool_growth.log 2>&1

Each run: 30-min WS sample (added to cache) + filter at WORKING tier
($2M / $250k) + add up to ADD_PER_RUN new whales. Quiet no-op once
pool hits target.

Premium tier ($5M / $500k) graduations are handled by
whale_graduation_scan.py (weekly cadence — that file gets reviewed by
Roy before any whale promotion).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from bots.structure.config import (
    WHALE_FLIP_THRESHOLD_USD,
    WHALE_LIST_TARGET_SIZE,
    WHALE_MIN_HISTORICAL_WIN_RATE,
)
from bots.structure.venue import HyperliquidVenue
from scripts.curate_whales_v2 import (
    CACHE_DIR,
    collect_candidates_from_ws,
    expand_via_counterparties,
    filter_and_rank,
)


WL_PATH = Path("bots/structure/whale_list.json")

# Working tier — what the live bot uses
WORKING_MIN_WIN_RATE = WHALE_MIN_HISTORICAL_WIN_RATE  # 0.60
WORKING_MIN_TRADES = 20
WORKING_MIN_CUMULATIVE_NOTIONAL_USD = 2_000_000.0
WORKING_MIN_CURRENT_POSITION_USD = WHALE_FLIP_THRESHOLD_USD  # 250_000.0
WORKING_TIER_LABEL = "$2M_$250k"

# Premium tier — original Item #7 spec, used here only for tagging
PREMIUM_MIN_CUMULATIVE_NOTIONAL_USD = 5_000_000.0
PREMIUM_MIN_CURRENT_POSITION_USD = 500_000.0
PREMIUM_TIER_LABEL = "$5M_$500k"


def _classify_tier(stats) -> str:
    if (
        stats.cumulative_notional_usd >= PREMIUM_MIN_CUMULATIVE_NOTIONAL_USD
        and stats.current_max_position_usd >= PREMIUM_MIN_CURRENT_POSITION_USD
    ):
        return PREMIUM_TIER_LABEL
    return WORKING_TIER_LABEL


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-seconds", type=int, default=1800)
    parser.add_argument("--top-n-assets", type=int, default=30)
    parser.add_argument("--candidate-pool", type=int, default=2500)
    parser.add_argument("--target", type=int, default=WHALE_LIST_TARGET_SIZE)
    parser.add_argument("--add-per-run", type=int, default=10,
                        help="Cap new whales added per run (default 10)")
    parser.add_argument("--no-counterparty", action="store_true")
    args = parser.parse_args()

    print(f"\n=== Whale pool growth — "
          f"{datetime.now(timezone.utc).isoformat()} ===\n", file=sys.stderr)

    # Load existing whale_list.json
    existing: dict = {"whales": []}
    if WL_PATH.exists():
        try:
            existing = json.loads(WL_PATH.read_text())
        except Exception as e:
            print(f"[error] failed to read {WL_PATH}: {e}", file=sys.stderr)
            return 1
    existing_whales = existing.get("whales", []) or []
    existing_addrs = {(w.get("address") or "").lower() for w in existing_whales}
    print(f"[state] existing pool: {len(existing_whales)} whales", file=sys.stderr)

    if len(existing_whales) >= args.target:
        print(f"[no-op] pool already at target ({args.target}); skipping sample.",
              file=sys.stderr)
        return 0

    # Phase 1: load accumulated cache + run fresh sample
    user_volume: dict[str, float] = {}
    if CACHE_DIR.exists():
        for cf in sorted(CACHE_DIR.glob("*.json")):
            try:
                cached = json.loads(cf.read_text())
                for addr, vol in cached.get("user_volume", {}).items():
                    user_volume[addr.lower()] = user_volume.get(addr.lower(), 0.0) + float(vol)
            except Exception:
                pass
    print(f"[cache] {len(user_volume)} addresses from accumulated samples",
          file=sys.stderr)

    if args.sample_seconds > 0:
        from bots.structure.config import get_structure_settings
        settings = get_structure_settings()
        print(f"\n[ws] running fresh {args.sample_seconds}s sample...",
              file=sys.stderr)
        fresh = collect_candidates_from_ws(
            api_url=settings.hyperliquid_api_url,
            top_n_assets=args.top_n_assets,
            sample_seconds=args.sample_seconds,
        )
        # Persist sample for future runs
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        cache_path = CACHE_DIR / f"sample_{ts}.json"
        cache_path.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sample_seconds": args.sample_seconds,
            "top_n_assets": args.top_n_assets,
            "user_volume": fresh,
            "source": "pool_growth",
        }))
        print(f"[cache] saved sample → {cache_path}", file=sys.stderr)
        for addr, vol in fresh.items():
            user_volume[addr.lower()] = user_volume.get(addr.lower(), 0.0) + vol

    if not user_volume:
        print("[error] no candidates available", file=sys.stderr)
        return 2

    # Phase 2: filter candidates we DON'T already have
    sorted_by_volume = sorted(user_volume.items(), key=lambda kv: kv[1], reverse=True)
    candidates_top = [a for a, _ in sorted_by_volume[: args.candidate_pool]]
    # Skip already-known addresses to save API calls
    candidates = [a for a in candidates_top if a.lower() not in existing_addrs]

    if not args.no_counterparty:
        venue = HyperliquidVenue()
        seeds = [a for a, _ in sorted_by_volume[:30]]
        print(f"\n[counterparty] expanding from top-{len(seeds)}...", file=sys.stderr)
        cps = expand_via_counterparties(venue, seeds, max_fills_per_seed=50)
        n_added = 0
        for addr in cps:
            if addr not in existing_addrs and addr not in {c.lower() for c in candidates}:
                candidates.append(addr)
                n_added += 1
        print(f"[counterparty] added {n_added} new candidates", file=sys.stderr)

    print(f"\n[total] evaluating {len(candidates)} NEW candidates "
          f"(skipped {len(existing_addrs)} known whales)", file=sys.stderr)

    # Phase 3: filter at working tier
    venue = HyperliquidVenue()
    qualified = filter_and_rank(
        candidates, venue,
        min_win_rate=WORKING_MIN_WIN_RATE,
        min_trades=WORKING_MIN_TRADES,
        min_cumulative_notional_usd=WORKING_MIN_CUMULATIVE_NOTIONAL_USD,
        min_current_position_usd=WORKING_MIN_CURRENT_POSITION_USD,
    )

    # Cap at add_per_run + remaining headroom to target
    headroom = max(0, args.target - len(existing_whales))
    n_to_add = min(args.add_per_run, headroom, len(qualified))
    new_whales = qualified[:n_to_add]
    print(f"\n[adds] qualified={len(qualified)}, headroom={headroom}, "
          f"add_per_run={args.add_per_run} → adding {len(new_whales)}", file=sys.stderr)

    if not new_whales:
        print(f"[done] no new whales added; pool stays at {len(existing_whales)}",
              file=sys.stderr)
        return 0

    # Phase 4: append to whale_list.json
    additions = []
    for s in new_whales:
        additions.append({
            "address": s.address,
            "tag": s.tag,
            "tier": _classify_tier(s),
            "added_at": datetime.now(timezone.utc).isoformat(),
            "historical_win_rate": round(s.win_rate, 4),
            "closed_positions_6mo": s.closed_positions,
            "cumulative_notional_usd": round(s.cumulative_notional_usd, 2),
            "avg_hold_minutes": round(s.avg_hold_minutes, 1),
            "current_max_position_usd": round(s.current_max_position_usd, 2),
        })

    existing_whales.extend(additions)
    out_obj = {
        **existing,
        "version": 2,
        "last_growth_run_at": datetime.now(timezone.utc).isoformat(),
        "filters": {
            "working_tier": {
                "min_win_rate": WORKING_MIN_WIN_RATE,
                "min_trades": WORKING_MIN_TRADES,
                "min_cumulative_notional_usd": WORKING_MIN_CUMULATIVE_NOTIONAL_USD,
                "min_current_position_usd": WORKING_MIN_CURRENT_POSITION_USD,
            },
            "premium_tier": {
                "min_cumulative_notional_usd": PREMIUM_MIN_CUMULATIVE_NOTIONAL_USD,
                "min_current_position_usd": PREMIUM_MIN_CURRENT_POSITION_USD,
            },
        },
        "whales": existing_whales,
    }
    WL_PATH.write_text(json.dumps(out_obj, indent=2))

    print(f"\n[done] added {len(additions)} new whales — pool now at "
          f"{len(existing_whales)}/{args.target}", file=sys.stderr)
    for w in additions:
        print(f"  + {w['address'][:10]}.. tier={w['tier']} "
              f"wr={w['historical_win_rate']:.2f} "
              f"vol=${w['cumulative_notional_usd']:,.0f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
