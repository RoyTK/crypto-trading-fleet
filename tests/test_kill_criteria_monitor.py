"""Tests for kill_criteria_monitor — trigger evaluation + transition alerting.

Mocks the DB session layer; exercises the threshold logic and alert-emit
transitions directly. No real DB / Redis required.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from framework import kill_criteria_monitor as kcm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_session(pnls=None, weekly_counts=None, slip_ratio=None, slip_abs=None, slip_n=0):
    """Build a MagicMock that simulates `with session_scope() as s:` behaviour.

    pnls: list[float] returned for the trades query
    weekly_counts: list[int] returned for the signals weekly aggregation
    slip_*: scalar values returned by the slippage query
    """
    pnls = pnls or []
    weekly_counts = weekly_counts or []

    session_cm = MagicMock()
    session = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False

    # Build a side-effect that responds to each .execute(text(...)) by inspecting
    # the SQL text and returning the right shape.
    def execute_side_effect(sql, *args, **kwargs):
        sql_str = str(sql).lower()
        result = MagicMock()
        if "from trades" in sql_str and "mode='paper'" in sql_str:
            result.all.return_value = [MagicMock(pnl_usd=p) for p in pnls]
        elif "from signals" in sql_str:
            result.all.return_value = [MagicMock(cnt=c) for c in weekly_counts]
        elif "from trades" in sql_str and "mode='shadow'" in sql_str:
            row = MagicMock()
            row.ratio = slip_ratio
            row.abs_bps = slip_abs
            row.n = slip_n
            result.first.return_value = row
        else:
            result.all.return_value = []
            result.first.return_value = None
        return result

    session.execute.side_effect = execute_side_effect
    return session_cm


# ---------------------------------------------------------------------------
# STRUCTURE criteria
# ---------------------------------------------------------------------------

def test_structure_empty_data_no_triggers():
    with patch.object(kcm, "session_scope", return_value=_mock_session()), \
         patch.object(kcm, "_paper_capital_for", return_value=10_000.0):
        status = kcm._compute_structure_status()
    assert status["n"] == 0
    assert status["kill_triggers"] == []
    assert status["warning_triggers"] == []
    assert status["promote_eligible"] is False


def test_structure_n_below_threshold_skips_wr_evaluation():
    """At N=30, WR<floor must NOT fire — too small to draw conclusions."""
    pnls = [-1.0] * 25 + [1.0] * 5  # WR = 5/30 = 0.166 — would trigger if N gate ignored
    with patch.object(kcm, "session_scope", return_value=_mock_session(pnls=pnls)), \
         patch.object(kcm, "_paper_capital_for", return_value=10_000.0):
        status = kcm._compute_structure_status()
    assert status["n"] == 30
    assert "structure_wr_below_floor" not in status["kill_triggers"]


def test_structure_wr_below_floor_at_n_50_fires():
    pnls = [-1.0] * 28 + [1.0] * 22  # WR = 22/50 = 0.44 (< floor 0.45)
    with patch.object(kcm, "session_scope", return_value=_mock_session(pnls=pnls)), \
         patch.object(kcm, "_paper_capital_for", return_value=10_000.0):
        status = kcm._compute_structure_status()
    assert status["n"] == 50
    assert status["wr"] == 0.44
    assert "structure_wr_below_floor" in status["kill_triggers"]


def test_structure_wr_within_warning_margin_fires_warning_not_kill():
    """WR = 0.49 is above floor 0.45 but within 10% margin (floor * 1.10 = 0.495)."""
    pnls = [-1.0] * 25 + [10.0] * 25 + [-1.0]  # WR = 25/51 ≈ 0.49
    # Actually need exactly 0.49 of N=50 → 24.5; use 49 trades 24 wins
    pnls = [-1.0] * 26 + [1.0] * 24  # N=50, WR=0.48
    with patch.object(kcm, "session_scope", return_value=_mock_session(pnls=pnls)), \
         patch.object(kcm, "_paper_capital_for", return_value=10_000.0):
        status = kcm._compute_structure_status()
    assert status["n"] == 50
    assert status["wr"] == 0.48
    assert "structure_wr_below_floor" not in status["kill_triggers"]
    assert "structure_wr_within_margin" in status["warning_triggers"]


def test_structure_signal_rate_3_quiet_weeks_fires():
    weekly_counts = [0, 0, 0, 5, 5, 5]  # last 3 weeks quiet
    with patch.object(kcm, "session_scope", return_value=_mock_session(weekly_counts=weekly_counts)), \
         patch.object(kcm, "_paper_capital_for", return_value=10_000.0):
        status = kcm._compute_structure_status()
    assert status["consecutive_quiet_weeks"] == 3
    assert "structure_signal_rate_quiet" in status["kill_triggers"]


def test_structure_slippage_excess_fires_only_with_min_sample():
    """Slippage criterion requires N>=10 shadow trades."""
    # First case: N=5, ratio is 3.0 (way over) — should NOT fire (too few samples)
    with patch.object(kcm, "session_scope",
                      return_value=_mock_session(slip_ratio=3.0, slip_abs=200.0, slip_n=5)), \
         patch.object(kcm, "_paper_capital_for", return_value=10_000.0):
        status = kcm._compute_structure_status()
    assert "structure_slippage_exceeds_threshold" not in status["kill_triggers"]

    # Second case: N=15, ratio 2.0 (>1.5 max) — should fire
    with patch.object(kcm, "session_scope",
                      return_value=_mock_session(slip_ratio=2.0, slip_abs=40.0, slip_n=15)), \
         patch.object(kcm, "_paper_capital_for", return_value=10_000.0):
        status = kcm._compute_structure_status()
    assert "structure_slippage_exceeds_threshold" in status["kill_triggers"]


# ---------------------------------------------------------------------------
# COPY criteria
# ---------------------------------------------------------------------------

def test_copy_pnl_below_floor_at_n_60_fires():
    """Each trade losing $1 → 60 trades = -$60 = -0.6% on $10k → below 2% floor."""
    pnls = [-1.0] * 30 + [1.0] * 30  # WR = 0.5, net PnL = 0 = 0% → kill (< +2% floor)
    with patch.object(kcm, "session_scope", return_value=_mock_session(pnls=pnls)), \
         patch.object(kcm, "_paper_capital_for", return_value=10_000.0):
        status = kcm._compute_copy_status()
    assert status["n"] == 60
    assert status["net_pnl_pct"] == 0.0
    assert "copy_pnl_below_floor" in status["kill_triggers"]


def test_copy_wr_above_floor_pnl_above_floor_no_kill():
    """WR=0.55, big winners — should be safe."""
    pnls = [-1.0] * 27 + [10.0] * 33  # N=60, WR=0.55, net = -27+330 = +303 = 3.03%
    with patch.object(kcm, "session_scope", return_value=_mock_session(pnls=pnls)), \
         patch.object(kcm, "_paper_capital_for", return_value=10_000.0):
        status = kcm._compute_copy_status()
    assert status["n"] == 60
    assert status["wr"] == 0.55
    assert status["net_pnl_pct"] > 2.0
    assert status["kill_triggers"] == []


def test_copy_promotion_eligible_when_all_met():
    """WR>=55%, PnL>=5%, Sharpe>=1.0, N>=60."""
    # 60 trades: 33 wins of +$20 each, 27 losses of -$5 each
    # Net = 660 - 135 = $525 = 5.25% on $10k. WR = 33/60 = 0.55.
    pnls = [-5.0] * 27 + [20.0] * 33
    with patch.object(kcm, "session_scope", return_value=_mock_session(pnls=pnls)), \
         patch.object(kcm, "_paper_capital_for", return_value=10_000.0):
        status = kcm._compute_copy_status()
    assert status["wr"] == 0.55
    assert status["net_pnl_pct"] >= 5.0
    # Sharpe will be very high with these unrealistically tight stats —
    # the point is just that ALL criteria pass simultaneously.
    assert status["sharpe"] is not None
    assert status["promote_eligible"] is True


# ---------------------------------------------------------------------------
# Regression: SQL queries must filter by exit_at, not entry_at
# ---------------------------------------------------------------------------
# 2026-05-26: original code used entry_at >= window_start which undercounted
# trades that entered before the window but closed during it. dd_monitor uses
# trailing-24h on exit_at; kill_criteria must use the same semantic for
# consistency. This test locks the behavior so it can't silently regress.

def test_structure_query_filters_by_exit_at_not_entry_at():
    captured_sql: list[str] = []

    class _CapturingSession:
        def execute(self, sql, *args, **kwargs):
            captured_sql.append(str(sql))
            r = MagicMock()
            r.all.return_value = []
            r.first.return_value = None
            return r

    session_cm = MagicMock()
    session_cm.__enter__.return_value = _CapturingSession()
    session_cm.__exit__.return_value = False
    with patch.object(kcm, "session_scope", return_value=session_cm), \
         patch.object(kcm, "_paper_capital_for", return_value=10_000.0):
        kcm._compute_structure_status()

    trade_queries = [s for s in captured_sql if "from trades" in s.lower()]
    assert trade_queries, "expected at least one query against trades"
    for q in trade_queries:
        ql = q.lower()
        # The WHERE clause must use exit_at as the window boundary.
        assert "exit_at >= :ws" in ql, (
            f"trade query must filter by exit_at (was: {q})"
        )
        # The original buggy pattern.
        assert "entry_at >= :ws" not in ql, (
            f"trade query must NOT filter by entry_at (was: {q})"
        )


def test_copy_query_filters_by_exit_at_not_entry_at():
    captured_sql: list[str] = []

    class _CapturingSession:
        def execute(self, sql, *args, **kwargs):
            captured_sql.append(str(sql))
            r = MagicMock()
            r.all.return_value = []
            r.first.return_value = None
            return r

    session_cm = MagicMock()
    session_cm.__enter__.return_value = _CapturingSession()
    session_cm.__exit__.return_value = False
    with patch.object(kcm, "session_scope", return_value=session_cm), \
         patch.object(kcm, "_paper_capital_for", return_value=10_000.0):
        kcm._compute_copy_status()

    trade_queries = [s for s in captured_sql if "from trades" in s.lower()]
    assert trade_queries, "expected at least one query against trades"
    for q in trade_queries:
        ql = q.lower()
        assert "exit_at >= :ws" in ql, (
            f"copy trade query must filter by exit_at (was: {q})"
        )
        assert "entry_at >= :ws" not in ql, (
            f"copy trade query must NOT filter by entry_at (was: {q})"
        )


# ---------------------------------------------------------------------------
# Transition alerting
# ---------------------------------------------------------------------------

def test_no_alert_when_trigger_persists_across_checks():
    """Once a criterion fires, subsequent checks should NOT re-alert."""
    prior = {"kill_triggers": ["structure_wr_below_floor"], "warning_triggers": [], "promote_eligible": False, "window": {"day_of_window": 10}, "wr": 0.4, "n": 50, "net_pnl_pct": -2.0}
    current = {"kill_triggers": ["structure_wr_below_floor"], "warning_triggers": [], "promote_eligible": False, "window": {"day_of_window": 11}, "wr": 0.39, "n": 51, "net_pnl_pct": -2.5}
    with patch.object(kcm, "emit_alert") as m, patch.object(kcm, "write_audit"):
        kcm._emit_transition_alerts("structure", prior, current)
    # No alerts — trigger was already in prior set
    assert m.call_count == 0


def test_alert_on_new_trigger():
    prior = {"kill_triggers": [], "warning_triggers": [], "promote_eligible": False, "window": {"day_of_window": 10}, "wr": 0.5, "n": 50, "net_pnl_pct": 0.5}
    current = {"kill_triggers": ["structure_wr_below_floor"], "warning_triggers": [], "promote_eligible": False, "window": {"day_of_window": 11}, "wr": 0.44, "n": 51, "net_pnl_pct": -1.0}
    with patch.object(kcm, "emit_alert") as m, patch.object(kcm, "write_audit"):
        kcm._emit_transition_alerts("structure", prior, current)
    assert m.call_count == 1
    args = m.call_args.kwargs
    assert args["event_type"] == "kill_criterion_fired"
    assert "structure_wr_below_floor" in args["title"]


def test_alert_on_promotion_transition():
    prior = {"kill_triggers": [], "warning_triggers": [], "promote_eligible": False, "window": {"day_of_window": 45}, "wr": 0.54, "n": 60, "net_pnl_pct": 4.5, "sharpe": 0.9}
    current = {"kill_triggers": [], "warning_triggers": [], "promote_eligible": True, "window": {"day_of_window": 46}, "wr": 0.56, "n": 61, "net_pnl_pct": 5.2, "sharpe": 1.1}
    with patch.object(kcm, "emit_alert") as m, patch.object(kcm, "write_audit"):
        kcm._emit_transition_alerts("copy", prior, current)
    assert m.call_count == 1
    args = m.call_args.kwargs
    assert args["event_type"] == "promotion_criteria_met"


def test_no_double_alert_when_both_kill_and_warning_would_fire():
    """If kill fires, warning should NOT also fire for the same criterion class."""
    prior = {"kill_triggers": [], "warning_triggers": [], "promote_eligible": False, "window": {"day_of_window": 10}, "wr": 0.5, "n": 50, "net_pnl_pct": 0.5}
    current = {"kill_triggers": ["structure_wr_below_floor"], "warning_triggers": ["structure_wr_below_floor"], "promote_eligible": False, "window": {"day_of_window": 11}, "wr": 0.40, "n": 51, "net_pnl_pct": -2.0}
    with patch.object(kcm, "emit_alert") as m, patch.object(kcm, "write_audit"):
        kcm._emit_transition_alerts("structure", prior, current)
    # Exactly one alert — the kill, not also the warning
    assert m.call_count == 1
    assert m.call_args.kwargs["event_type"] == "kill_criterion_fired"
