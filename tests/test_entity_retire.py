"""Retired wallets/teams must never trade (2026-09-29): watch_tag returns None for them."""
from bots.copy import loop_helpers


def _status(value):
    return lambda strategy, entity: value


def test_watch_tag_active(monkeypatch):
    monkeypatch.setattr(loop_helpers, "get_entity_status", _status("active"))
    assert loop_helpers.watch_tag("swing", "W1") == "swing"


def test_watch_tag_watch(monkeypatch):
    monkeypatch.setattr(loop_helpers, "get_entity_status", _status("watch"))
    assert loop_helpers.watch_tag("conviction", "W1") == "conviction_watch"


def test_watch_tag_retired_blocks(monkeypatch):
    monkeypatch.setattr(loop_helpers, "get_entity_status", _status("retired"))
    assert loop_helpers.watch_tag("cohortfire", "9") is None


def test_cluster_track_probation_rule():
    from bots.copy.loop_helpers import cluster_track
    assert cluster_track({"A": 1, "B": 1, "C": 1}, set(), 3) == "cluster"
    assert cluster_track({"A": 1, "B": 1, "P": 1}, {"P"}, 3) == "cluster_watch"      # needs P to reach 3
    assert cluster_track({"A": 1, "B": 1, "C": 1, "P": 1}, {"P"}, 3) == "cluster"    # 3 proven anyway
    assert cluster_track(["A", "P"], {"P"}, 3) == "cluster_watch"
