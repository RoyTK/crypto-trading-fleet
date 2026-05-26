"""Whale position-size distribution — for principled whale_flip threshold tuning.

Pulls current open positions for every wallet in `structure_whale_pool` via
the Hyperliquid Info API, aggregates the distribution of position notional
sizes, and prints a histogram + thresholds-table so Roy can pick a data-grounded
WHALE_FLIP_THRESHOLD_USD.

Run on Hetzner (where HL API is reachable + DB is accessible):

    docker compose exec scoring python -m scripts.whale_position_distribution

Output: histogram + suggested thresholds for various target signal rates.

Note: this measures POSITION SIZE distribution. whale_flip's actual trigger
is FLIP SIZE = abs(prior_notional) + abs(current_notional), so it'll be
~1.5-2x larger than typical position size when a real flip happens (a whale
going from +X to -Y has flip_size = X+Y). Position-size distribution is a
reasonable proxy — a $250k flip threshold is roughly equivalent to whales
holding $150-200k+ positions.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from sqlalchemy import text

from framework.db import session_scope
from framework.logging_setup import configure_logging, get_logger


configure_logging()
log = get_logger(__name__)


THRESHOLDS_USD = [50_000, 75_000, 100_000, 150_000, 200_000, 250_000, 350_000, 500_000, 750_000, 1_000_000]


def _fetch_whale_addresses() -> list[str]:
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT address FROM structure_whale_pool WHERE deleted_at IS NULL"
        )).all()
    return [r.address for r in rows]


def _fetch_positions_for(address: str, info) -> list[dict[str, Any]]:
    """Return list of {asset, notional_usd_abs, side} for whale's open positions."""
    try:
        state = info.user_state(address)
    except Exception:
        log.warning("user_state_failed", address=address)
        return []
    positions = []
    for ap in state.get("assetPositions", []) or []:
        p = ap.get("position", {})
        asset = p.get("coin")
        szi = p.get("szi")  # signed size (negative = short)
        entry_px = p.get("entryPx")
        if asset is None or szi is None or entry_px is None:
            continue
        try:
            sz = float(szi)
            px = float(entry_px)
        except (TypeError, ValueError):
            continue
        if sz == 0:
            continue
        notional_abs = abs(sz) * px
        positions.append({
            "asset": asset,
            "notional_usd_abs": notional_abs,
            "side": "long" if sz > 0 else "short",
        })
    return positions


def main() -> None:
    addresses = _fetch_whale_addresses()
    print(f"\nFetching positions for {len(addresses)} whales from structure_whale_pool...\n")

    from hyperliquid.info import Info
    from framework.config import get_settings
    settings = get_settings()
    info = Info(getattr(settings, "hyperliquid_api_url", "https://api.hyperliquid.xyz"), skip_ws=True)

    all_positions: list[dict[str, Any]] = []
    whales_with_positions = 0
    for addr in addresses:
        positions = _fetch_positions_for(addr, info)
        if positions:
            whales_with_positions += 1
        all_positions.extend(positions)

    print(f"Whales with at least one open position: {whales_with_positions}/{len(addresses)}")
    print(f"Total open positions across pool: {len(all_positions)}\n")

    if not all_positions:
        print("No positions found — nothing to analyze.")
        return

    sizes = sorted([p["notional_usd_abs"] for p in all_positions], reverse=True)

    # Histogram with log-ish buckets
    print("Position-size distribution (open positions only):")
    print("-" * 60)
    buckets = [
        ("$0 - $25k",       0,         25_000),
        ("$25k - $50k",     25_000,    50_000),
        ("$50k - $100k",    50_000,    100_000),
        ("$100k - $150k",   100_000,   150_000),
        ("$150k - $250k",   150_000,   250_000),
        ("$250k - $500k",   250_000,   500_000),
        ("$500k - $1M",     500_000,   1_000_000),
        ("$1M - $5M",       1_000_000, 5_000_000),
        ("$5M+",            5_000_000, math.inf),
    ]
    total = len(sizes)
    for name, lo, hi in buckets:
        count = sum(1 for s in sizes if lo <= s < hi)
        pct = 100.0 * count / total if total else 0.0
        bar = "#" * int(pct / 2)  # 2% per char
        print(f"  {name:<18s} {count:5d}  ({pct:5.1f}%)  {bar}")
    print()

    # Threshold table — "if WHALE_FLIP_THRESHOLD = X, how many positions qualify"
    print("Positions at-or-above each threshold (current pool snapshot):")
    print("-" * 60)
    print(f"  {'Threshold':>12s}  {'Count':>6s}  {'Pct':>6s}  {'Avg notional above':>20s}")
    for thr in THRESHOLDS_USD:
        qualifying = [s for s in sizes if s >= thr]
        n = len(qualifying)
        pct = 100.0 * n / total if total else 0.0
        avg = sum(qualifying) / n if n else 0.0
        print(f"  ${thr:>10,d}  {n:6d}  {pct:5.1f}%  ${avg:>18,.0f}")
    print()

    # Quick percentile summary
    if sizes:
        def pctl(p: float) -> float:
            idx = int(round((1 - p / 100) * (len(sizes) - 1)))  # sizes is desc
            return sizes[max(0, min(idx, len(sizes) - 1))]
        print("Percentiles (positions sorted by size, descending):")
        for p in [50, 75, 90, 95, 99]:
            print(f"  p{p:>2d} (top {100-p}% of positions): ${pctl(p):>14,.0f}")
        print()

    # Suggested thresholds for various target signal rates
    # whale_flip fires when a whale flips direction with abs(prior_notional)
    # >= threshold. The position-size distribution is a proxy: a position
    # of size X CAN produce a flip of size X+Y where Y is the new opposite
    # position. So flip_threshold ~ position_threshold isn't 1:1 but close
    # enough for ballpark.
    print("Suggested principled thresholds (rough — see script docstring):")
    print("-" * 60)
    pool_size = whales_with_positions if whales_with_positions else 1
    # Assume each whale changes direction (flips) ~1x per N days on average.
    # Without historical flip data we estimate from observed signal rate:
    # current threshold $250k → 1.2 signals/day → ~5% of positions qualify
    # (rough — the 19 whale_flip signals over 16 days came from a pool of 50)
    # → flip rate per qualifying position ≈ 1.2/day ÷ ~5%*50 ≈ 0.5/day per whale
    target_rates = [
        ("conservative — match funding_fade WR-quality bar", 3),
        ("moderate — ~5 signals/day, fits 60-day N=60 target", 5),
        ("aggressive — 10/day, dataset doubles in 2 weeks",   10),
    ]
    flip_per_qualifying_per_day = 0.5  # rough estimate
    for label, target_per_day in target_rates:
        needed_qualifying = target_per_day / flip_per_qualifying_per_day
        # Find the threshold where qualifying count is close to that
        best_thr = None
        best_diff = math.inf
        for thr in THRESHOLDS_USD:
            qual = sum(1 for s in sizes if s >= thr)
            if abs(qual - needed_qualifying) < best_diff:
                best_diff = abs(qual - needed_qualifying)
                best_thr = thr
        print(f"  Target {target_per_day}/day ({label}):")
        print(f"    → threshold ≈ ${best_thr:,} (qualifying positions: {sum(1 for s in sizes if s >= best_thr)})")
    print()
    print("Note: flip-per-position-per-day estimate (0.5) is rough; consider")
    print("backing this out from actual whale_flip signal history if you want")
    print("more precision before committing to a new threshold value.")


if __name__ == "__main__":
    main()
