"""Regression guard for the promobuy flatline exit (2026-07-23).

Thresholds from promobuy_flatline_study (467 trades): of 26 promobuy trades that survived
>=2 days, 22 flatlined (never ran +50%) and 88-95% of those rugged to ~-83% while sitting
~breakeven at the 2-day mark. So exit an aged + never-ran + STILL-LIQUID promobuy position
to recover ~breakeven before the pull. This is the LIQUID complement to is_stagnant_illiquid_reap
(which cuts the already-dead sub-floor tokens): the flatline exit fires at liq >= floor, the
reaper at liq < floor. ⚠ Unlike the reaper it CAN cut a liquid 1322-type late-runner.
"""
from __future__ import annotations

from bots.copy.loop_helpers import is_flatline_exit, is_stagnant_illiquid_reap

KW = dict(min_age_hours=48.0, max_peak_pct=50.0, min_liq_usd=10000.0)


def test_exits_old_flat_still_liquid_promobuy():
    # aged + never-ran + still sellable ($10k+) -> exit to recover before the rug
    assert is_flatline_exit("promobuy", 60.0, 5.0, 25_000.0, **KW) is True


def test_exits_retired_zombie_tag():
    assert is_flatline_exit("promobuy_pre_reset2", 200.0, 0.0, 15_000.0, **KW) is True


def test_spares_thin_pool_that_is_the_reapers_job():
    # below the liq floor: too late to recover for a gain -> the reaper handles it, not us
    assert is_flatline_exit("promobuy", 100.0, 0.0, 400.0, **KW) is False


def test_spares_position_that_ran():
    # peak >= max_peak_pct: it already ran -> trailing/ladder owns it
    assert is_flatline_exit("promobuy", 100.0, 120.0, 25_000.0, **KW) is False


def test_spares_young_position():
    assert is_flatline_exit("promobuy", 24.0, 0.0, 25_000.0, **KW) is False


def test_no_exit_without_liquidity_reading():
    # None liq (oracle gap this cycle) must NOT force a close
    assert is_flatline_exit("promobuy", 100.0, 0.0, None, **KW) is False


def test_only_promobuy_family():
    for other in ("cluster", "conviction", "teamfollow", "cohortfire", None):
        assert is_flatline_exit(other, 100.0, 0.0, 25_000.0, **KW) is False


def test_boundary_age_peak_and_liq():
    # exactly at the age threshold exits; exactly at the flat-peak ceiling does NOT (ran enough)
    assert is_flatline_exit("promobuy", 48.0, 49.9, 25_000.0, **KW) is True
    assert is_flatline_exit("promobuy", 48.0, 50.0, 25_000.0, **KW) is False
    # exactly at the liq floor DOES exit (>= is inclusive — still sellable)
    assert is_flatline_exit("promobuy", 48.0, 0.0, 10_000.0, **KW) is True


def test_flatline_and_reaper_partition_by_liquidity():
    # The two rules cover disjoint liquidity ranges for the same aged/flat token:
    # reaper handles < floor, flatline handles >= floor. Never both, never neither
    # (for an aged, never-ran promobuy position with a real liq reading).
    rkw = dict(min_age_hours=48.0, max_flat_peak_pct=50.0, min_liq_usd=10000.0)
    for liq in (500.0, 9_999.0, 10_000.0, 30_000.0, 250_000.0):
        reap = is_stagnant_illiquid_reap("promobuy", 100.0, 0.0, liq, **rkw)
        flat = is_flatline_exit("promobuy", 100.0, 0.0, liq, **KW)
        assert reap != flat, f"liq={liq}: exactly one rule must fire (reap={reap}, flat={flat})"
