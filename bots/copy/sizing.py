"""COPY position sizing.

Pure function — no DB, no network. Tested in isolation.

Inputs:
- cluster_size: number of distinct wallets that triggered the cluster (≥3)
- paper_capital_usd: bot's notional balance
- current_open_alloc_pct: % of paper capital already in open positions
- current_dd_today_pct: today's drawdown (positive number; 12 = at-the-halt)

Output: notional_usd

Locked rules (Item #7):
- 3 wallets → 4%, 4-5 → 6%, 6+ → 8% of paper capital
- Per-trade cap 8%
- Allocation cap 50% — if current open + this trade > 50%, shrink/skip
- DD discount: as drawdown approaches the daily halt, size shrinks to 0.5x floor
"""
from __future__ import annotations

from bots.copy.config import (
    ALLOCATION_CAP_PCT,
    PER_TRADE_NOTIONAL_CAP_PCT,
    cluster_size_to_pct,
    get_copy_settings,
)


def size_position(
    cluster_size: int,
    paper_capital_usd: float,
    current_open_alloc_pct: float = 0.0,
    current_dd_today_pct: float = 0.0,
) -> float:
    """Return the notional USD to place. Returns 0 if the trade should be skipped."""
    base_pct = cluster_size_to_pct(cluster_size)
    if base_pct <= 0:
        return 0.0

    # Per-trade cap
    base_pct = min(base_pct, PER_TRADE_NOTIONAL_CAP_PCT)

    # Allocation cap — if adding this would exceed 50% open, shrink to fit
    headroom_pct = ALLOCATION_CAP_PCT - current_open_alloc_pct
    if headroom_pct <= 0:
        return 0.0
    final_pct = min(base_pct, headroom_pct)

    notional_usd = paper_capital_usd * (final_pct / 100.0)

    # Drawdown discount: linear shrink from 1.0 (no DD) to 0.5 (at halt)
    settings = get_copy_settings()
    dd_halt_pct = settings.copy_dd_daily_pct
    if current_dd_today_pct > 0 and dd_halt_pct > 0:
        dd_ratio = min(current_dd_today_pct / dd_halt_pct, 1.0)
        discount = max(0.5, 1.0 - dd_ratio * 0.5)
        notional_usd *= discount

    return notional_usd


def size_conviction_position(
    paper_capital_usd: float,
    current_open_alloc_pct: float = 0.0,
    current_dd_today_pct: float = 0.0,
) -> float:
    """Size a single-wallet "conviction" trade. Returns notional USD (0 = skip).

    Unlike the cluster sizer there is no cluster_size → % mapping: every
    conviction trigger uses a flat `copy_conviction_sizing_pct` (default 4%,
    same as a 3-wallet cluster). The allocation cap + DD discount mirror the
    cluster sizer, but are computed against the conviction bankroll and the
    conviction-only open allocation.
    """
    settings = get_copy_settings()
    base_pct = settings.copy_conviction_sizing_pct
    if base_pct <= 0:
        return 0.0

    cap_pct = settings.copy_conviction_alloc_cap_pct
    headroom_pct = cap_pct - current_open_alloc_pct
    if headroom_pct <= 0:
        return 0.0
    final_pct = min(base_pct, headroom_pct)

    notional_usd = paper_capital_usd * (final_pct / 100.0)

    dd_halt_pct = settings.copy_dd_daily_pct
    if current_dd_today_pct > 0 and dd_halt_pct > 0:
        dd_ratio = min(current_dd_today_pct / dd_halt_pct, 1.0)
        discount = max(0.5, 1.0 - dd_ratio * 0.5)
        notional_usd *= discount

    return notional_usd


def size_teamfollow_position(
    paper_capital_usd: float,
    current_open_alloc_pct: float = 0.0,
    current_dd_today_pct: float = 0.0,
) -> float:
    """Size a team-follow trade. Returns notional USD (0 = skip).

    FLAT sizing (like conviction, NOT the cluster band) — team-follow's min_members
    is 2, so the cluster_size→% map returns 0 for it (cluster requires >=3). Flat
    also matches the backtest's equal-size-per-signal model. Uses
    `copy_teamfollow_sizing_pct` against the teamfollow bankroll + alloc cap.
    """
    settings = get_copy_settings()
    base_pct = settings.copy_teamfollow_sizing_pct
    if base_pct <= 0:
        return 0.0

    cap_pct = settings.copy_teamfollow_alloc_cap_pct
    headroom_pct = cap_pct - current_open_alloc_pct
    if headroom_pct <= 0:
        return 0.0
    final_pct = min(base_pct, headroom_pct)

    notional_usd = paper_capital_usd * (final_pct / 100.0)

    dd_halt_pct = settings.copy_dd_daily_pct
    if current_dd_today_pct > 0 and dd_halt_pct > 0:
        dd_ratio = min(current_dd_today_pct / dd_halt_pct, 1.0)
        discount = max(0.5, 1.0 - dd_ratio * 0.5)
        notional_usd *= discount

    return notional_usd
