"""Guard the teamfollow watch/promote-demote isolation (2026-07-24).

A demoted team's trades are tagged strategy='teamfollow_watch'. That tag MUST be isolated
from the live 'teamfollow' family (so watch losses/positions never leak into the live
bankroll, dd or dashboards) AND must have its own bucket (so open_allocation_pct('teamfollow_watch')
counts only watch). This tests the _strategy_clause SQL fragments that enforce it.
"""
from __future__ import annotations

from bots.copy.loop_helpers import _strategy_clause


def _sql(strategy):
    c = _strategy_clause(strategy)
    return str(c) if c is not None else None


def test_teamfollow_excludes_watch_and_pre_reset():
    sql = _sql("teamfollow")
    assert "LIKE 'teamfollow%'" in sql
    assert "NOT LIKE 'teamfollow_watch%'" in sql
    assert "NOT LIKE 'teamfollow_pre_reset%'" in sql


def test_teamfollow_watch_is_its_own_exact_bucket():
    sql = _sql("teamfollow_watch")
    assert sql == "coalesce(sim_metadata->>'strategy','') = 'teamfollow_watch'"


def test_cluster_bucket_excludes_teamfollow_family_including_watch():
    # cluster excludes all 'teamfollow%', which covers teamfollow_watch too
    sql = _sql("cluster")
    assert "NOT LIKE 'teamfollow%'" in sql


def test_watch_pattern_generalizes_to_other_families():
    assert _sql("promobuy_watch") == "coalesce(sim_metadata->>'strategy','') = 'promobuy_watch'"
    assert "NOT LIKE 'promobuy_watch%'" in _sql("promobuy")
    assert "NOT LIKE 'cohortfire_watch%'" in _sql("cohortfire")


def test_conviction_and_none_unchanged():
    assert _sql("conviction") == "(sim_metadata->>'strategy') = 'conviction'"
    assert _sql(None) is None
