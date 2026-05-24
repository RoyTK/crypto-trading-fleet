"""Premium whale graduation scan (12-hour cron).

Sifts the accumulating WS sample cache for whales meeting the ORIGINAL Item #7
quality criteria (≥$5M cumulative notional, ≥$500k current position) — the
high-conviction tier. Whales that meet these stricter bars graduate from the
working pool ($250k/$2M) to a "premium candidates" review file.

This script is autonomous-safe:
- Runs WS sample (default 30 min) and saves to cache
- Re-filters accumulated cache at PREMIUM thresholds
- Writes whale_list_premium_candidates.json (NOT the live whale_list.json)
- Roy reviews and manually promotes premium candidates → live whale_list.json
- Idempotent — safe to re-run any time

Cron entry (Hetzner crontab as fleet user):
    0 */12 * * * cd /home/fleet/crypto-fleet && /usr/bin/docker compose exec -T \\
      framework python -m scripts.whale_graduation_scan \\
      >> /home/fleet/logs/whale_graduation.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from bots.structure.config import get_structure_settings
from bots.structure.venue import HyperliquidVenue
from scripts.curate_whales_v2 import (
    CACHE_DIR,
    collect_candidates_from_ws,
    expand_via_counterparties,
    filter_and_rank,
)


PREMIUM_OUT = Path("bots/structure/whale_list_premium_candidates.json")
WL_PATH = Path("bots/structure/whale_list.json")

# Premium tier — original Item #7 spec
PREMIUM_MIN_WIN_RATE = 0.60
PREMIUM_MIN_TRADES = 20
PREMIUM_MIN_CUMULATIVE_NOTIONAL_USD = 5_000_000.0
PREMIUM_MIN_CURRENT_POSITION_USD = 500_000.0
PREMIUM_TARGET = 50


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-seconds", type=int, default=1800,
                        help="Fresh WS sample duration (default 30 min). 0 = filter cache only.")
    parser.add_argument("--top-n-assets", type=int, default=30)
    parser.add_argument("--candidate-pool", type=int, default=2000,
                        help="How many top-volume addresses to evaluate (default 2000)")
    parser.add_argument("--target", type=int, default=PREMIUM_TARGET)
    parser.add_argument("--no-counterparty", action="store_true",
                        help="Skip counterparty expansion (faster but smaller pool)")
    args = parser.parse_args()

    settings = get_structure_settings()
    print(f"\n=== Premium whale graduation scan — "
          f"{datetime.now(timezone.utc).isoformat()} ===\n", file=sys.stderr)

    # ---- Phase 1: load accumulated cache ---------------------------------
    user_volume: dict[str, float] = {}
    if CACHE_DIR.exists():
        cache_files = sorted(CACHE_DIR.glob("*.json"))
        for cf in cache_files:
            try:
                cached = json.loads(cf.read_text())
                for addr, vol in cached.get("user_volume", {}).items():
                    user_volume[addr.lower()] = user_volume.get(addr.lower(), 0.0) + float(vol)
            except Exception as e:
                print(f"[cache] failed to load {cf}: {e}", file=sys.stderr)
        print(f"[cache] accumulated {len(cache_files)} prior samples → "
              f"{len(user_volume)} unique addresses", file=sys.stderr)

    # ---- Phase 2: fresh sample (saved to cache) --------------------------
    if args.sample_seconds > 0:
        print(f"\n[ws] running fresh {args.sample_seconds}s sample on top-{args.top_n_assets}",
              file=sys.stderr)
        fresh = collect_candidates_from_ws(
            api_url=settings.hyperliquid_api_url,
            top_n_assets=args.top_n_assets,
            sample_seconds=args.sample_seconds,
        )
        # Persist this sample for future graduation scans
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        cache_path = CACHE_DIR / f"sample_{ts}.json"
        cache_path.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sample_seconds": args.sample_seconds,
            "top_n_assets": args.top_n_assets,
            "user_volume": fresh,
            "source": "graduation_scan",
        }))
        print(f"[cache] saved this sample to {cache_path}", file=sys.stderr)
        for addr, vol in fresh.items():
            user_volume[addr.lower()] = user_volume.get(addr.lower(), 0.0) + vol

    if not user_volume:
        print("[error] no candidates available — cache empty and no sample run.",
              file=sys.stderr)
        return 2

    # ---- Phase 3: candidates from accumulated volume ---------------------
    sorted_by_volume = sorted(user_volume.items(), key=lambda kv: kv[1], reverse=True)
    candidates = [a for a, _ in sorted_by_volume[: args.candidate_pool]]
    candidate_set = {a.lower() for a in candidates}

    # Always include addresses already in the live structure_whale_pool table
    # (so demotions are visible — a live whale that no longer meets premium
    # bars will show up as not in the graduation candidates list).
    from sqlalchemy import select
    from framework.db import session_scope
    from framework.models import StructureWhalePool
    try:
        with session_scope() as s:
            for row in s.execute(
                select(StructureWhalePool.address).where(
                    StructureWhalePool.pruned_at.is_(None)
                )
            ).all():
                addr = (row[0] or "").lower()
                if addr and addr not in candidate_set:
                    candidates.append(addr)
                    candidate_set.add(addr)
    except Exception:
        pass

    # Optional counterparty expansion (off by default to keep cron fast)
    if not args.no_counterparty:
        venue = HyperliquidVenue()
        seeds = [a for a, _ in sorted_by_volume[:30]]
        print(f"\n[counterparty] expanding from top-{len(seeds)} candidates...", file=sys.stderr)
        cps = expand_via_counterparties(venue, seeds, max_fills_per_seed=50)
        n_added = 0
        for addr in cps:
            if addr not in candidate_set:
                candidates.append(addr)
                candidate_set.add(addr)
                n_added += 1
        print(f"[counterparty] added {n_added} new addresses", file=sys.stderr)

    print(f"\n[total] evaluating {len(candidates)} candidates against PREMIUM bars: "
          f"WR≥{PREMIUM_MIN_WIN_RATE}, trades≥{PREMIUM_MIN_TRADES}, "
          f"vol≥${PREMIUM_MIN_CUMULATIVE_NOTIONAL_USD:,.0f}, "
          f"cur_pos≥${PREMIUM_MIN_CURRENT_POSITION_USD:,.0f}", file=sys.stderr)

    venue = HyperliquidVenue()
    qualified = filter_and_rank(
        candidates, venue,
        min_win_rate=PREMIUM_MIN_WIN_RATE,
        min_trades=PREMIUM_MIN_TRADES,
        min_cumulative_notional_usd=PREMIUM_MIN_CUMULATIVE_NOTIONAL_USD,
        min_current_position_usd=PREMIUM_MIN_CURRENT_POSITION_USD,
    )
    top = qualified[: args.target]

    # ---- Phase 4: write review file --------------------------------------
    out_obj = {
        "version": 2,
        "tier": "premium",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidates_evaluated": len(candidates),
        "qualified_count": len(qualified),
        "filters": {
            "min_win_rate": PREMIUM_MIN_WIN_RATE,
            "min_trades": PREMIUM_MIN_TRADES,
            "min_cumulative_notional_usd": PREMIUM_MIN_CUMULATIVE_NOTIONAL_USD,
            "min_current_position_usd": PREMIUM_MIN_CURRENT_POSITION_USD,
        },
        "review_instructions": (
            "These whales meet the premium ($5M / $500k) bars. To promote any "
            "into the live pool, manually merge their entry into "
            "bots/structure/whale_list.json and restart bot_structure."
        ),
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
    PREMIUM_OUT.parent.mkdir(parents=True, exist_ok=True)
    PREMIUM_OUT.write_text(json.dumps(out_obj, indent=2))
    print(f"\n[done] {len(top)} premium-tier candidates written to {PREMIUM_OUT}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
