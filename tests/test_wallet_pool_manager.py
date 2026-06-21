"""Unit tests for wallet_pool_manager.decide_tier_changes.

Pure-logic — no DB, no network. Constructs WalletSnapshot fixtures with
known timestamps + event counts and asserts the right transitions fire.
"""
import os
from datetime import datetime, timedelta, timezone

# Avoid pulling in framework settings that require Postgres at import time
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://nope:nope@localhost/nope")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from bots.copy.wallet_pool_manager import (
    WalletSnapshot,
    decide_tier_changes,
)


NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _w(
    address: str,
    tier: str = "watch",
    *,
    added_days_ago: int = 30,
    last_event_days_ago: int | None = 1,
    events_30d: int = 0,
    events_7d: int = 0,
    events_48h: int = 0,
    cielo_winrate_90d: float | None = 0.7,
    last_attribution_days_ago: int | None = None,
    pinned: bool = False,
):
    return WalletSnapshot(
        address=address,
        tier=tier,
        added_at=NOW - timedelta(days=added_days_ago),
        last_event_at=(
            None if last_event_days_ago is None
            else NOW - timedelta(days=last_event_days_ago)
        ),
        events_30d=events_30d,
        events_7d=events_7d,
        events_48h=events_48h,
        cielo_winrate_90d=cielo_winrate_90d,
        last_attribution_at=(
            None if last_attribution_days_ago is None
            else NOW - timedelta(days=last_attribution_days_ago)
        ),
        pinned=pinned,
    )


# ---------- Promotion --------------------------------------------------------

def test_promote_qualifying_watch_when_active_has_headroom():
    wallets = [
        _w("w1", tier="watch", events_7d=10, events_30d=40, cielo_winrate_90d=0.7),
    ]
    d = decide_tier_changes(wallets, now=NOW)
    assert "w1" in d.promote
    assert not d.demote


def test_dont_promote_below_events_7d_bar():
    wallets = [
        _w("w1", tier="watch", events_7d=3, events_30d=40, cielo_winrate_90d=0.7),
    ]
    d = decide_tier_changes(wallets, now=NOW)
    assert "w1" not in d.promote


def test_dont_promote_below_winrate_bar():
    wallets = [
        _w("w1", tier="watch", events_7d=10, events_30d=40, cielo_winrate_90d=0.40),
    ]
    d = decide_tier_changes(wallets, now=NOW)
    assert "w1" not in d.promote


def test_promote_when_winrate_is_null():
    """Null cielo_winrate_90d → allow promotion (data not yet collected)."""
    wallets = [
        _w("w1", tier="watch", events_7d=10, events_30d=40, cielo_winrate_90d=None),
    ]
    d = decide_tier_changes(wallets, now=NOW)
    assert "w1" in d.promote


def test_dont_promote_when_active_at_cap_no_demotions():
    actives = [
        _w(f"a{i}", tier="active", events_30d=50, events_7d=10) for i in range(75)
    ]
    candidate = _w("hot", tier="watch", events_7d=20, events_30d=40, cielo_winrate_90d=0.8)
    # target=75 so the 75 actives are at cap (default is now 125)
    d = decide_tier_changes(actives + [candidate], now=NOW, active_list_target=75)
    # No room, no swap-in eligibility (candidate's events_48h=0 < 10)
    assert "hot" not in d.promote
    assert not d.swap_in


# ---------- Demotion ---------------------------------------------------------

def test_demote_active_below_events_30d_bar():
    wallets = [
        _w("a1", tier="active", events_30d=5),  # below 10 threshold
    ]
    d = decide_tier_changes(wallets, now=NOW)
    assert "a1" in d.demote


def test_dont_demote_active_meeting_bar():
    wallets = [
        _w("a1", tier="active", events_30d=50),
    ]
    d = decide_tier_changes(wallets, now=NOW)
    assert "a1" not in d.demote


def test_dont_demote_active_within_grace_window():
    """Newly-added active wallets (<30d ago) can't have 30d of events.
    Don't demote them — they need time to prove themselves."""
    wallets = [
        _w("a1", tier="active", added_days_ago=10, events_30d=0),
    ]
    d = decide_tier_changes(wallets, now=NOW)
    assert "a1" not in d.demote


def test_demote_active_past_grace_with_no_events():
    """After grace expires, low-events_30d → demote (no protection)."""
    wallets = [
        _w("a1", tier="active", added_days_ago=45, events_30d=0),
    ]
    d = decide_tier_changes(wallets, now=NOW)
    assert "a1" in d.demote


def test_attribution_protection_within_1yr_blocks_demotion():
    wallets = [
        _w("a1", tier="active", events_30d=2, last_attribution_days_ago=100),
    ]
    d = decide_tier_changes(wallets, now=NOW)
    assert "a1" not in d.demote


def test_attribution_protection_expires_after_1yr():
    wallets = [
        _w("a1", tier="active", events_30d=2, last_attribution_days_ago=400),
    ]
    d = decide_tier_changes(wallets, now=NOW)
    assert "a1" in d.demote, "attribution > 1yr old should not protect"


# ---------- Drop -------------------------------------------------------------

def test_drop_watch_silent_for_90d():
    wallets = [
        _w("w1", tier="watch", added_days_ago=120, last_event_days_ago=100),
    ]
    d = decide_tier_changes(wallets, now=NOW)
    assert "w1" in d.drop


def test_dont_drop_recently_added_even_if_silent():
    """New wallets get a full 90d window before drop eligibility."""
    wallets = [
        _w("w1", tier="watch", added_days_ago=10, last_event_days_ago=None),
    ]
    d = decide_tier_changes(wallets, now=NOW)
    assert "w1" not in d.drop


def test_dont_drop_active_silent_wallets_only_watch():
    wallets = [
        _w("a1", tier="active", added_days_ago=120, last_event_days_ago=100,
           events_30d=0),
    ]
    d = decide_tier_changes(wallets, now=NOW)
    assert "a1" in d.demote  # demoted, not dropped
    assert "a1" not in d.drop


# ---------- Pinning ----------------------------------------------------------

def test_pinned_active_immune_from_demotion():
    wallets = [
        _w("a1", tier="active", events_30d=0, pinned=True),
    ]
    d = decide_tier_changes(wallets, now=NOW)
    assert "a1" not in d.demote


def test_pinned_watch_immune_from_drop():
    wallets = [
        _w("w1", tier="watch", added_days_ago=120, last_event_days_ago=100,
           pinned=True),
    ]
    d = decide_tier_changes(wallets, now=NOW)
    assert "w1" not in d.drop


def test_pinned_watch_not_auto_promoted():
    """Pinned status doesn't auto-promote — only manual promotion via DB."""
    wallets = [
        _w("w1", tier="watch", events_7d=20, events_30d=40,
           cielo_winrate_90d=0.8, pinned=True),
    ]
    d = decide_tier_changes(wallets, now=NOW)
    assert "w1" not in d.promote


# ---------- Targeted swap-in -------------------------------------------------

def test_swap_in_when_active_full_and_hot_watch_appears():
    actives = [
        _w(f"a{i}", tier="active", events_30d=20 + i, events_7d=5)
        for i in range(75)
    ]
    hot = _w(
        "hot", tier="watch",
        events_7d=15, events_30d=15, events_48h=20,  # ≥10 in 48h
        cielo_winrate_90d=0.8,
    )
    # swap_in is OFF by default now (2026-06-21) — must opt in. target=75 so
    # the 75 actives are "at cap" (default target is 125).
    d = decide_tier_changes(actives + [hot], now=NOW,
                            enable_swap_in=True, active_list_target=75)
    assert len(d.swap_in) == 1
    promote_addr, demote_addr = d.swap_in[0]
    assert promote_addr == "hot"
    # The weakest active (a0 with events_30d=20) should be displaced
    assert demote_addr == "a0"


def test_no_swap_in_below_48h_event_bar():
    actives = [
        _w(f"a{i}", tier="active", events_30d=20) for i in range(75)
    ]
    candidate = _w(
        "warm", tier="watch", events_7d=10, events_30d=15,
        events_48h=5,  # below 10 threshold
        cielo_winrate_90d=0.8,
    )
    d = decide_tier_changes(actives + [candidate], now=NOW,
                            enable_swap_in=True, active_list_target=75)
    assert not d.swap_in


def test_swap_in_skips_attribution_protected_displacement_target():
    """When picking a victim, attribution-protected actives must not be candidates."""
    actives = []
    # a0 has lowest events but is attribution-protected
    actives.append(_w("a0", tier="active", events_30d=10, last_attribution_days_ago=100))
    for i in range(1, 75):
        actives.append(_w(f"a{i}", tier="active", events_30d=20 + i))
    hot = _w(
        "hot", tier="watch", events_7d=15, events_30d=15, events_48h=20,
        cielo_winrate_90d=0.8,
    )
    d = decide_tier_changes(actives + [hot], now=NOW,
                            enable_swap_in=True, active_list_target=75)
    # a0 (protected) should NOT be the displaced wallet; next-weakest a1 is
    assert len(d.swap_in) == 1
    _, demote_addr = d.swap_in[0]
    assert demote_addr == "a1"


# ---------- swap_in disabled by default (2026-06-21) ------------------------

def test_swap_in_off_by_default():
    """The activity-ranked swap churned HFT noise into active; it's now OFF
    unless explicitly enabled."""
    actives = [_w(f"a{i}", tier="active", events_30d=20 + i, events_7d=5)
               for i in range(75)]
    hot = _w("hot", tier="watch", events_7d=15, events_30d=15, events_48h=20,
             cielo_winrate_90d=0.8)
    d = decide_tier_changes(actives + [hot], now=NOW, active_list_target=75)
    assert d.swap_in == []


def test_swap_in_capped_when_enabled():
    """Even enabled, swaps are capped per run to prevent mass churn."""
    actives = [_w(f"a{i}", tier="active", events_30d=20 + i) for i in range(75)]
    hots = [_w(f"hot{i}", tier="watch", events_7d=15, events_30d=15,
               events_48h=20, cielo_winrate_90d=0.8) for i in range(20)]
    d = decide_tier_changes(actives + hots, now=NOW, enable_swap_in=True,
                            active_list_target=75, max_swaps_per_run=5)
    assert len(d.swap_in) == 5  # capped, not 20


# ---------- Promotion cap (2026-06-21) --------------------------------------

def test_promotions_capped_per_run():
    """After a mass demotion, headroom is large but promotions are capped
    so active refills gradually, not flooded with unproven wallets."""
    # No actives → headroom = full target (125), far above the cap
    watches = [_w(f"w{i}", tier="watch", events_7d=10 + i, events_30d=40,
                  cielo_winrate_90d=0.7) for i in range(30)]
    d = decide_tier_changes(watches, now=NOW, max_promotions_per_run=10)
    assert len(d.promote) == 10  # capped, not 30


# ---------- PnL-based demotion (2026-06-21) ---------------------------------

def test_demote_active_wallet_net_negative_after_enough_trades():
    """A noisy money-loser (high events, negative attributed PnL) gets
    demoted — the path the old events_30d<10 rule could never reach."""
    w = _w("loser", tier="active", events_30d=200_000)  # HFT-level activity
    w.attributed_trades = 108
    w.attributed_pnl_usd = -96.6
    d = decide_tier_changes([w], now=NOW)
    assert "loser" in d.demote


def test_high_frequency_winner_is_kept():
    """9ZPsRWG-class: very high activity BUT net-positive PnL → KEEP.
    An event-count HFT filter would wrongly kill this; PnL-based doesn't."""
    w = _w("winner", tier="active", events_30d=45_000)  # ~1500 swaps/day
    w.attributed_trades = 20
    w.attributed_pnl_usd = 421.0
    d = decide_tier_changes([w], now=NOW)
    assert "winner" not in d.demote


def test_pnl_demotion_needs_minimum_trades():
    """A net-negative wallet with too few trades isn't demoted yet (noise)."""
    w = _w("newish", tier="active", events_30d=5000)
    w.attributed_trades = 3  # below the 5-trade bar
    w.attributed_pnl_usd = -20.0
    d = decide_tier_changes([w], now=NOW)
    assert "newish" not in d.demote


def test_pnl_demotion_bypasses_attribution_protection():
    """A proven LOSER is demoted even if attribution-protected (the
    protection guards quiet WINNERS, not money-losers)."""
    w = _w("protected_loser", tier="active", events_30d=100_000,
           last_attribution_days_ago=10)  # recent attribution = protected
    w.attributed_trades = 50
    w.attributed_pnl_usd = -54.0
    d = decide_tier_changes([w], now=NOW)
    assert "protected_loser" in d.demote


def test_pnl_demotion_respects_pinned():
    """Pinned wallets (9ZPsRWG) are immune even if PnL is negative on the
    contaminated sample."""
    w = _w("pinned_loser", tier="active", events_30d=100_000, pinned=True)
    w.attributed_trades = 50
    w.attributed_pnl_usd = -54.0
    d = decide_tier_changes([w], now=NOW)
    assert "pinned_loser" not in d.demote


# ---------- Composite scenario ----------------------------------------------

def test_full_daily_cycle_mixed_transitions():
    wallets = [
        # Active wallet to demote (low events, no attribution)
        _w("a_demote", tier="active", events_30d=3),
        # Active wallet protected (recent attribution)
        _w("a_protected", tier="active", events_30d=2,
           last_attribution_days_ago=30),
        # Active wallet that stays (good events)
        _w("a_stay", tier="active", events_30d=50),
        # Watch wallet to promote (qualifies + headroom)
        _w("w_promote", tier="watch", events_7d=8, events_30d=30,
           cielo_winrate_90d=0.7),
        # Watch wallet to drop (silent 100d)
        _w("w_drop", tier="watch", added_days_ago=120, last_event_days_ago=100),
        # Watch wallet that stays (still in 90d window)
        _w("w_stay", tier="watch", added_days_ago=30, last_event_days_ago=5,
           events_30d=2),
    ]
    d = decide_tier_changes(wallets, now=NOW)
    assert "a_demote" in d.demote
    assert "a_protected" not in d.demote
    assert "a_stay" not in d.demote
    assert "w_promote" in d.promote
    assert "w_drop" in d.drop
    assert "w_stay" not in d.drop
    assert "w_stay" not in d.promote  # events_7d too low
