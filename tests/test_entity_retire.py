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
