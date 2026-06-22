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
ACTIVE_LIST_TARGET = 125  # Roy 2026-06: intentionally >75 (see config.copy_active_list_target)
DEMOTE_EVENTS_30D_BELOW = 10
PROMOTE_EVENTS_7D_AT_LEAST = 5
PROMOTE_MIN_WIN_RATE = 0.55  # Cielo 90d winrate (0-1 ratio)
DROP_DAYS_SILENT = 90
ATTRIBUTION_PROTECTION_DAYS = 365
TARGETED_SWAP_IN_EVENTS_48H = 10
# New wallets get a 30d grace period before demotion can fire. Otherwise
# wallets added < 30 days ago are unfairly punished — their events_30d
# can't have accumulated yet. Same logic protects against demoting the
# entire pool on day 1 of the Phase A migration.
DEMOTE_GRACE_DAYS = 30

# PnL-based demotion (2026-06-21 fix). The original logic ONLY demoted
# QUIET wallets (events_30d < 10), so noisy money-LOSERS (HFT/MM bots that
# fire constant cluster signals) could never be removed — they spammed
# losing trades forever. Demote any active wallet that has copied enough
# trades to judge AND is net-negative. Criterion is MONEY, not activity:
# our best wallet (9ZPsRWG) does ~1500 swaps/day and is hugely positive —
# an event-count "HFT filter" would have killed it. This bypasses the
# attribution-protection window (that window protects proven WINNERS from
# quiet-period demotion; a proven LOSER should still go).
DEMOTE_MIN_ATTRIBUTED_TRADES = 5
DEMOTE_ATTRIBUTED_PNL_BELOW_USD = 0.0

# swap_in is DISABLED by default (2026-06-21 fix). It was an
# observation-phase mechanism to force "hot" (= most active) watch wallets
# into a full active list, displacing the weakest-by-events active. With
# active permanently over target and the watch pool full of HFT
# birdeye_gainers wallets, it mass-churned 24-92 wallets/day, kept active
# stuffed with HFT noise, and thrashed the Helius webhook (status=400). The
# promote/demote/PnL path is now the clean selection mechanism; swap_in is
# gated off and capped if ever re-enabled.
ENABLE_SWAP_IN = False
MAX_SWAPS_PER_RUN = 5

# Vetted-only promotion (2026-06-22, Roy). The active list should be VETTED
# wallets only — the legacy/auto-discovery wallets were never held to the
# realized-PnL/short-hold/not-bag-holder standard, and vetting found ~94%
# of auto-discovered wallets are noise. So only promote wallets whose source
# is browser_opus* (curated or vetted). Unvetted wallets sit in watch until
# browser-Opus vets them (KEEP->vetted->promotable, REJECT->pruned).
PROMOTE_VETTED_ONLY = True


def _is_vetted(source: Optional[str]) -> bool:
    return (source or "").lower().startswith("browser_opus")

# Cap promotions per daily run (2026-06-21). After the one-time mass
# PnL-demotion of the bloated HFT active list, active drops far below
# target and promote would otherwise re-flood it with ~50 UNPROVEN watch
# wallets in a single day — recreating the losing-trade spam via the
# promote path. Cap the daily influx so the pool refills gradually and new
# wallets get a chance to prove (or fail) on attributed PnL before more
# flood in. Watch wallets have no attributed PnL yet, so promotion is still
# activity-ranked; the cap + PnL-demotion together make that safe.
MAX_PROMOTIONS_PER_RUN = 10


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
    # Attributed-trade PnL for this wallet (2026-06-21). Drives PnL-based
    # demotion. attributed_trades = number of closed copied trades this
    # wallet participated in; attributed_pnl_usd = sum of its equal-share
    # attribution. Default 0/0 = no copied trades yet (can't judge → keep).
    attributed_trades: int = 0
    attributed_pnl_usd: float = 0.0
    # Most-negative single attributed trade (2026-06-21). Used so one bad
    # trade (e.g. a rug) can't condemn an otherwise-positive wallet — see
    # _is_proven_loser. 0.0 default = no losing trade recorded.
    worst_attributed_pnl_usd: float = 0.0
    # Discovery source — used to prioritize promotion (2026-06-21). Vetted
    # browser_opus curated wallets promote ahead of raw birdeye_gainers.
    source: Optional[str] = None


def _is_proven_loser(w: "WalletSnapshot", min_trades: int, pnl_floor: float) -> bool:
    """A wallet that has copied enough trades to judge AND is ROBUSTLY
    net-negative — negative even after excluding its single worst trade.

    Criterion is MONEY, not activity (a high-frequency winner like 9ZPsRWG is
    NOT a loser). The exclude-worst-trade rule encodes "one rug doesn't make
    a loser" (Roy): a wallet that's +$100 across its trades but dropped to
    -$33 by a single -$133 rug is NOT a loser — excluding that trade it's
    +$100. A wallet that's still negative after dropping its worst trade IS a
    robust loser (CreQJ2t9, MfDuWeq, the turtle cluster)."""
    if w.attributed_trades < min_trades:
        return False
    if w.attributed_pnl_usd >= pnl_floor:
        return False
    # Negative even without the single worst (most-negative) trade?
    net_excluding_worst = w.attributed_pnl_usd - min(0.0, w.worst_attributed_pnl_usd)
    return net_excluding_worst < 0


def _source_priority(source: Optional[str]) -> int:
    """Promotion ordering: vetted browser_opus curated wallets first, then
    legacy migration, then raw leaderboard scrapes (birdeye_gainers)."""
    s = (source or "").lower()
    if s.startswith("browser_opus"):
        return 0
    if s.startswith("legacy_migration"):
        return 1
    return 2


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
    demote_grace_days: int = DEMOTE_GRACE_DAYS,
    demote_min_attributed_trades: int = DEMOTE_MIN_ATTRIBUTED_TRADES,
    demote_attributed_pnl_below_usd: float = DEMOTE_ATTRIBUTED_PNL_BELOW_USD,
    enable_swap_in: bool = ENABLE_SWAP_IN,
    max_swaps_per_run: int = MAX_SWAPS_PER_RUN,
    max_promotions_per_run: int = MAX_PROMOTIONS_PER_RUN,
    promote_vetted_only: bool = PROMOTE_VETTED_ONLY,
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

    # ---- Step 1: drop silent OR proven-loser watch wallets ---------------
    silent_cutoff = now - timedelta(days=drop_days_silent)
    for w in watches:
        if w.pinned:
            continue
        # 1a. Proven-loser drop (2026-06-21). A watch wallet that already
        # copied enough trades and lost money gets PRUNED — not left in
        # watch to be re-promoted by activity ranking. This is the fix for
        # the loser-cycle: high-activity losers (CreQJ2t9 -$117/166 trades,
        # MfDuWeq -$116/186, Ddwfjf -$332/31) were demoted to watch then
        # re-promoted daily because they're the most active. Pruning
        # unsubscribes them entirely so the cycle stops.
        if _is_proven_loser(w, demote_min_attributed_trades, demote_attributed_pnl_below_usd):
            drop.append(w.address)
            continue
        # 1b. Silent drop (original): no events in drop_days_silent days.
        # Gate on "added before cutoff" so new wallets get a full window.
        if w.added_at > silent_cutoff:
            continue
        if w.last_event_at is None or w.last_event_at < silent_cutoff:
            drop.append(w.address)

    # ---- Step 2: demote underperforming active wallets -------------------
    grace_cutoff = now - timedelta(days=demote_grace_days)
    protection_cutoff = now - timedelta(days=attribution_protection_days)
    for w in actives:
        if w.pinned:
            continue

        # 2a. PnL-based demotion (2026-06-21). A wallet that is a ROBUST
        # net-loser (negative even excluding its single worst trade) gets
        # demoted — regardless of activity or attribution protection. This
        # removes noisy money-losers (HFT/MM bots) the events_30d<10 rule
        # never could. Criterion is MONEY not activity (9ZPsRWG stays), and
        # the exclude-worst rule means one rug can't condemn an otherwise-
        # positive wallet.
        if _is_proven_loser(w, demote_min_attributed_trades, demote_attributed_pnl_below_usd):
            demote.append(w.address)
            continue

        # 2b. Quiet-wallet demotion (original rule): events_30d below bar.
        if w.events_30d >= demote_events_30d_below:
            continue
        # Grace period: newly-added wallets can't have accumulated 30d of
        # events yet. Skip them until they've had a fair shot.
        if w.added_at > grace_cutoff:
            continue
        # Attribution-protected? Only if last_attribution_at is recent.
        # (Applies only to the QUIET path — a proven loser in 2a is demoted
        # regardless of protection.)
        if w.last_attribution_at is not None and w.last_attribution_at >= protection_cutoff:
            continue
        demote.append(w.address)

    # ---- Step 3: promote qualifying watch wallets (up to headroom) -------
    # Recompute remaining active count after demotions
    remaining_actives = [w for w in actives if w.address not in demote]
    headroom = max(0, active_list_target - len(remaining_actives))

    # Eligible: watch wallets with enough recent activity + WR validation,
    # EXCLUDING proven losers (don't re-admit a wallet that already lost
    # money — the core fix for the loser-cycle).
    eligible_promotions = [
        w for w in watches
        if w.address not in drop
        and not w.pinned  # pinned watch wallets shouldn't auto-promote
        and w.events_7d >= promote_events_7d_at_least
        and (w.cielo_winrate_90d is None or w.cielo_winrate_90d >= promote_min_win_rate)
        and not _is_proven_loser(w, demote_min_attributed_trades, demote_attributed_pnl_below_usd)
        and (not promote_vetted_only or _is_vetted(w.source))
    ]
    # Rank by SOURCE first (vetted browser_opus ahead of raw birdeye_gainers),
    # then by recent activity within a source tier. Watch wallets have no
    # attributed PnL yet, so source quality is the best promotion signal we
    # have — promote the curated picks before the leaderboard noise.
    eligible_promotions.sort(key=lambda w: (_source_priority(w.source), -w.events_7d))

    # Cap the daily influx (see MAX_PROMOTIONS_PER_RUN): fill headroom but no
    # more than the per-run cap, so a post-cleanup active list refills
    # gradually instead of flooding with unproven wallets in one day.
    promote_budget = min(headroom, max_promotions_per_run)
    for w in eligible_promotions[:promote_budget]:
        promote.append(w.address)

    # ---- Step 4: targeted swap-in for hot watch wallets if active cap full
    # DISABLED by default (2026-06-21). This activity-ranked swap mass-
    # churned the active list with HFT noise and thrashed the Helius
    # webhook. The promote/demote/PnL path is the selection mechanism now.
    # Kept behind enable_swap_in + a per-run cap for optional future use.
    if enable_swap_in and len(remaining_actives) + len(promote) >= active_list_target:
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
            if len(swap_in) >= max_swaps_per_run:
                break  # cap churn even when enabled
            victim = displaceable.pop(0)
            swap_in.append((hot.address, victim.address))

    return TierDecisions(
        promote=promote,
        demote=demote,
        drop=drop,
        swap_in=swap_in,
    )
