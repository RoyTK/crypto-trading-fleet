"""Pure-logic promote/demote/drop decisions for the COPY wallet pool.

DB orchestration is in scripts/wallet_pool_daily_cron.py; this module
contains only the decision function so it's fully unit-testable.

Tier semantics:
  active  : subscribed to primary Helius webhook; events feed cluster detector
  watch   : subscribed to secondary Helius webhook; events tracked only
  pruned  : NOT subscribed to any webhook; row kept for re-discovery
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


# Default rule parameters (overridable per-call for testing). These match
# the locked plan: copy-wallet-active-watch-tier.md.
ACTIVE_LIST_TARGET = 75
DEMOTE_EVENTS_30D_BELOW = 10
PROMOTE_EVENTS_7D_AT_LEAST = 5
PROMOTE_MIN_WIN_RATE = 0.55  # Cielo 90d winrate (0-1 ratio)
DROP_DAYS_SILENT = 90
ATTRIBUTION_PROTECTION_DAYS = 365
TARGETED_SWAP_IN_EVENTS_48H = 10


@dataclass
class WalletSnapshot:
    """Subset of WalletPool fields needed for tier decisions."""
    address: str
    tier: str  # 'active' | 'watch' | 'pruned'
    added_at: datetime
    last_event_at: Optional[datetime]
    events_30d: int
    events_7d: int  # subset of events_30d; computed by caller
    events_48h: int  # subset of events_7d; for targeted swap-in
    cielo_winrate_90d: Optional[float]  # 0-1 ratio (not 0-100)
    last_attribution_at: Optional[datetime]
    pinned: bool


@dataclass
class TierDecisions:
    promote: list[str]  # watch → active
    demote: list[str]   # active → watch
    drop: list[str]     # watch → pruned
    swap_in: list[tuple[str, str]]  # (promote_address, demote_address) pairs


def decide_tier_changes(
    wallets: list[WalletSnapshot],
    *,
    now: Optional[datetime] = None,
    active_list_target: int = ACTIVE_LIST_TARGET,
    demote_events_30d_below: int = DEMOTE_EVENTS_30D_BELOW,
    promote_events_7d_at_least: int = PROMOTE_EVENTS_7D_AT_LEAST,
    promote_min_win_rate: float = PROMOTE_MIN_WIN_RATE,
    drop_days_silent: int = DROP_DAYS_SILENT,
    attribution_protection_days: int = ATTRIBUTION_PROTECTION_DAYS,
    targeted_swap_in_events_48h: int = TARGETED_SWAP_IN_EVENTS_48H,
) -> TierDecisions:
    """Decide the daily tier transitions.

    Order of operations:
      1. Drop watch wallets with 0 events in `drop_days_silent` days
      2. Demote active wallets failing the events-30d bar (unless protected)
      3. Promote watch wallets meeting the events-7d + WR bar (up to active cap)
      4. Targeted swap-in: any watch wallet with ≥10 events/48h displaces the
         weakest non-protected active wallet (used during observation phase)

    A wallet is "attribution-protected" from demotion if it has a
    `last_attribution_at` within the past `attribution_protection_days`. A
    wallet is "pinned" if its pinned flag is true — pinned wallets are
    immune from all transitions.
    """
    now = now or datetime.now(timezone.utc)

    promote: list[str] = []
    demote: list[str] = []
    drop: list[str] = []
    swap_in: list[tuple[str, str]] = []

    actives = [w for w in wallets if w.tier == "active"]
    watches = [w for w in wallets if w.tier == "watch"]

    # ---- Step 1: drop fully-silent watch wallets -------------------------
    silent_cutoff = now - timedelta(days=drop_days_silent)
    for w in watches:
        if w.pinned:
            continue
        # Wallet is silent if no events ever, OR last event before cutoff.
        # Also gate on "added before cutoff" so newly-added wallets get a
        # full window to prove themselves before being dropped.
        if w.added_at > silent_cutoff:
            continue
        if w.last_event_at is None or w.last_event_at < silent_cutoff:
            drop.append(w.address)

    # ---- Step 2: demote underperforming active wallets -------------------
    for w in actives:
        if w.pinned:
            continue
        if w.events_30d >= demote_events_30d_below:
            continue
        # Attribution-protected? Only if last_attribution_at is recent.
        if w.last_attribution_at is not None:
            protection_cutoff = now - timedelta(days=attribution_protection_days)
            if w.last_attribution_at >= protection_cutoff:
                continue
        demote.append(w.address)

    # ---- Step 3: promote qualifying watch wallets (up to headroom) -------
    # Recompute remaining active count after demotions
    remaining_actives = [w for w in actives if w.address not in demote]
    headroom = max(0, active_list_target - len(remaining_actives))

    # Eligible: watch wallets with enough recent activity + WR validation
    eligible_promotions = [
        w for w in watches
        if w.address not in drop
        and not w.pinned  # pinned watch wallets shouldn't auto-promote
        and w.events_7d >= promote_events_7d_at_least
        and (w.cielo_winrate_90d is None or w.cielo_winrate_90d >= promote_min_win_rate)
    ]
    # Rank by recent activity (events_7d desc) — promote the hottest first
    eligible_promotions.sort(key=lambda w: w.events_7d, reverse=True)

    for w in eligible_promotions[:headroom]:
        promote.append(w.address)

    # ---- Step 4: targeted swap-in for hot watch wallets if active cap full
    # If active list is full AND a watch wallet shows ≥10 events/48h AND
    # there exists a non-protected active wallet to displace, swap them.
    if len(remaining_actives) + len(promote) >= active_list_target:
        already_promoted = set(promote)
        hot_watches = [
            w for w in watches
            if w.address not in drop
            and w.address not in already_promoted
            and not w.pinned
            and w.events_48h >= targeted_swap_in_events_48h
            and (w.cielo_winrate_90d is None or w.cielo_winrate_90d >= promote_min_win_rate)
        ]
        hot_watches.sort(key=lambda w: w.events_48h, reverse=True)

        # Build displacement candidates from active: weakest first (low events_30d),
        # excluding pinned and attribution-protected (which we already filtered
        # out of `demote`).
        displaceable = []
        for w in actives:
            if w.address in demote:
                continue  # already going to be demoted
            if w.pinned:
                continue
            if w.last_attribution_at is not None:
                protection_cutoff = now - timedelta(days=attribution_protection_days)
                if w.last_attribution_at >= protection_cutoff:
                    continue
            displaceable.append(w)
        displaceable.sort(key=lambda w: w.events_30d)  # weakest first

        for hot in hot_watches:
            if not displaceable:
                break
            victim = displaceable.pop(0)
            swap_in.append((hot.address, victim.address))

    return TierDecisions(
        promote=promote,
        demote=demote,
        drop=drop,
        swap_in=swap_in,
    )
