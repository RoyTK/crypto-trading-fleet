"""Regression guard for the promobuy stagnant-illiquid reaper (2026-07-21).

Thresholds set from promobuy_stagnant_liq_study (459 tokens): of 356 flat-at-72h tokens,
only 1 ran >=2x afterward (0.3%), and it had $28.8k liquidity; 0 of 339 flat sub-$10k
tokens ran late. So reap old + never-ran + thin-pool positions, spare liquid ones (a
time-only cut would kill the 1322 case: flat 3 days then +313% on day 6).
"""
from __future__ import annotations

from bots.copy.loop_helpers import is_stagnant_illiquid_reap

KW = dict(min_age_hours=72.0, max_flat_peak_pct=50.0, min_liq_usd=10000.0)


def test_reaps_old_flat_illiquid_promobuy():
    assert is_stagnant_illiquid_reap("promobuy", 100.0, 5.0, 400.0, **KW) is True


def test_reaps_retired_zombie_tag():
    # the 21 stuck zombies carry retired tags but still start with 'promobuy'
    assert is_stagnant_illiquid_reap("promobuy_pre_reset2", 200.0, 0.0, 300.0, **KW) is True


def test_spares_liquid_position_the_1322_case():
    # flat + old but liquid (>= $10k) -> KEEP: the only late-runner in the study lived here
    assert is_stagnant_illiquid_reap("promobuy", 100.0, 10.0, 28_832.0, **KW) is False


def test_spares_position_that_ran():
    # peak >= max_flat_peak_pct means it already ran -> not our concern (trailing/ladder owns it)
    assert is_stagnant_illiquid_reap("promobuy", 100.0, 120.0, 400.0, **KW) is False


def test_spares_young_position():
    assert is_stagnant_illiquid_reap("promobuy", 24.0, 0.0, 400.0, **KW) is False


def test_no_reap_without_liquidity_reading():
    # None liq (oracle gap this cycle) must NOT force a close — wait for a real reading
    assert is_stagnant_illiquid_reap("promobuy", 100.0, 0.0, None, **KW) is False


def test_only_promobuy_family():
    for other in ("cluster", "conviction", "teamfollow", "cohortfire", None):
        assert is_stagnant_illiquid_reap(other, 100.0, 0.0, 400.0, **KW) is False


def test_boundary_age_and_peak():
    # exactly at the age threshold reaps; exactly at the flat-peak ceiling does NOT (ran enough)
    assert is_stagnant_illiquid_reap("promobuy", 72.0, 49.9, 400.0, **KW) is True
    assert is_stagnant_illiquid_reap("promobuy", 72.0, 50.0, 400.0, **KW) is False
    # exactly at the liq floor is NOT reaped (< is strict)
    assert is_stagnant_illiquid_reap("promobuy", 72.0, 0.0, 10_000.0, **KW) is False
