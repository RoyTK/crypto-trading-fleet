"""Regression guard for the cross-strategy isolation bug class.

Two of the worst incidents came from `_strategy_clause` not being family-aware:
the cluster clause was `NOT LIKE 'conviction%'`, so when promobuy/teamfollow/
cohortfire were added, cluster's allocation cap + per-token dedup counted THEIR
open positions — cluster silently sized to $0 for 4 days (`36988cf`). These pin
the family-scoping so that regression can't return when strategy #6 is added.
"""
from __future__ import annotations

from bots.copy.loop_helpers import _strategy_clause

OTHER_FAMILIES = ("conviction", "teamfollow", "cohortfire", "promobuy")


def test_cluster_clause_excludes_every_other_family():
    sql = str(_strategy_clause("cluster"))
    for fam in OTHER_FAMILIES:
        assert f"NOT LIKE '{fam}%'" in sql, f"cluster allocation must exclude {fam} (the leak bug)"


def test_sibling_clauses_are_own_active_prefix():
    # own-family prefix, but EXCLUDING the retired '_pre_reset' era (2026-07-21 fix: retired
    # zombies must not consume the live strategy's allocation cap) — and NOT excluding any
    # OTHER family (that would re-introduce the cross-strategy leak).
    for fam in ("teamfollow", "cohortfire", "promobuy"):
        sql = str(_strategy_clause(fam))
        assert f"LIKE '{fam}%'" in sql
        assert f"NOT LIKE '{fam}_pre_reset%'" in sql   # excludes its own retired era
        for other in ("conviction", "cluster", "teamfollow", "cohortfire", "promobuy"):
            if other != fam:
                assert f"NOT LIKE '{other}%'" not in sql  # never excludes a sibling family


def test_conviction_is_exact_active_tag():
    # exact match so retired 'conviction_pre_reset' rows never leak into conviction's scope
    assert "= 'conviction'" in str(_strategy_clause("conviction"))


def test_none_means_no_filter():
    assert _strategy_clause(None) is None


def test_no_clause_is_the_stale_two_strategy_relic():
    # the exact fingerprint of the retired bug: cluster == "not conviction, nothing else"
    cluster_sql = str(_strategy_clause("cluster"))
    # it must NOT be the bare 2-strategy clause (only excluding conviction)
    assert cluster_sql.count("NOT LIKE") >= 4
