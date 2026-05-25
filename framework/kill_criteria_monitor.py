"""Kill + promotion criteria monitor for STRUCTURE + COPY paper window.

Signed 2026-05-25 (see memory/project_decision_log.md). Window: 60d
primary (through 2026-07-24), auto-extend to 90d (2026-08-23) if N
target not reached by primary end.

CRITICAL DESIGN NOTE: Roy explicitly retains manual judgment on actual
kill/promote decisions. This module ALERTS only — no auto-halt. The
halt_bot() integration is deliberately omitted. If you're reading this
and thinking "we should wire this to dd_monitor to auto-halt," DON'T.
See decision log entry from 2026-05-25.

Called by scoring engine cron (every 60 min). Emits P1 Discord alerts
only on TRANSITION (criterion newly fires this run, or newly enters
within-warning margin) — not every check, to avoid spam.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

from framework.alerts import emit_alert
from framework.audit import write_audit
from framework.db import session_scope
from framework.logging_setup import get_logger
from framework.models import BotState
from monitoring.alerting.taxonomy import Severity


log = get_logger(__name__)


# ---------------------------------------------------------------------------
# WINDOW + CRITERIA (signed 2026-05-25, see project_decision_log.md)
# ---------------------------------------------------------------------------

WINDOW_START = datetime(2026, 5, 25, tzinfo=timezone.utc)
WINDOW_END_PRIMARY = datetime(2026, 7, 24, tzinfo=timezone.utc)   # +60d
WINDOW_END_EXTENDED = datetime(2026, 8, 23, tzinfo=timezone.utc)  # +90d auto-extend

# Trigger alerts when criterion is within this fraction of firing
WARNING_MARGIN = 0.10  # 10% margin


STRUCTURE_CRITERIA: dict[str, Any] = {
    "wr_floor": 0.45,                  # kill if closed WR < this at N>=wr_min_n
    "wr_min_n": 50,
    "signal_rate_min_per_week": 1.0,
    "signal_rate_quiet_weeks_threshold": 3,
    "slippage_ratio_max": 1.5,         # kill if realized/modeled > this
    "slippage_absolute_max_bps": 50.0, # OR realized > this absolute (Build B only)
}


COPY_CRITERIA: dict[str, Any] = {
    "wr_floor": 0.47,
    "wr_min_n": 60,
    "pnl_floor_pct": 2.0,              # kill if net PnL < +2% of paper capital at N>=wr_min_n
    "wallet_tier_filter": "active",    # only count active-tier signals
}


PROMOTION_CRITERIA: dict[str, dict[str, float]] = {
    "structure": {"wr": 0.55, "pnl_pct": 5.0, "sharpe": 1.0, "slippage_ratio_max": 1.2},
    "copy": {"wr": 0.55, "pnl_pct": 5.0, "sharpe": 1.0},
}


# ---------------------------------------------------------------------------
# Status computation
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _window_metadata() -> dict[str, Any]:
    now = _now()
    primary_remaining = (WINDOW_END_PRIMARY - now).days
    extended_remaining = (WINDOW_END_EXTENDED - now).days
    day_of_window = (now - WINDOW_START).days
    return {
        "start": WINDOW_START.isoformat(),
        "end_primary": WINDOW_END_PRIMARY.isoformat(),
        "end_extended": WINDOW_END_EXTENDED.isoformat(),
        "day_of_window": day_of_window,
        "days_remaining_primary": primary_remaining,
        "days_remaining_extended": extended_remaining,
        "phase": "primary" if now < WINDOW_END_PRIMARY else ("extended" if now < WINDOW_END_EXTENDED else "ended"),
    }


def _paper_capital_for(bot_id: str) -> float:
    """Read paper_capital from env vars directly.

    NOTE 2026-05-25: do NOT import from bots.*.config here. The scoring
    container only mounts ./framework — `bots/` is not on its path. The
    previous import-based version (and dd_monitor's parallel one) silently
    fell back to a hardcoded default in production for that reason. Env
    vars are loaded into every container via .env, so this is reliable.
    """
    import os
    env_key = {
        "structure": "STRUCTURE_PAPER_CAPITAL_USD",
        "copy": "COPY_PAPER_CAPITAL_USD",
    }.get(bot_id)
    if env_key:
        raw = os.environ.get(env_key)
        if raw:
            try:
                return float(raw)
            except ValueError:
                log.warning("kill_criteria_paper_capital_invalid", bot_id=bot_id, raw=raw)
    return 10_000.0  # fallback matches current $10k both bots


def _sharpe(pnls: list[float]) -> Optional[float]:
    """Annualized-ish Sharpe on per-trade PnL. Returns None if N<5 or stdev=0."""
    if len(pnls) < 5:
        return None
    mean = statistics.mean(pnls)
    sd = statistics.stdev(pnls)
    if sd == 0:
        return None
    # Per-trade Sharpe scaled by sqrt(N_trades_per_year). With STRUCTURE
    # ~50 trades expected in 60-day window = ~300/yr → sqrt(300) ≈ 17.3.
    # COPY at higher rate may be ~600/yr → sqrt(600) ≈ 24.5. Use 17 as
    # conservative shared scaler; not academically rigorous but consistent.
    return (mean / sd) * math.sqrt(17.0)


def _compute_structure_status() -> dict[str, Any]:
    paper_capital = _paper_capital_for("structure")
    window_start = WINDOW_START

    with session_scope() as s:
        # Closed paper trades — WR + PnL
        rows = s.execute(text(
            """
            SELECT pnl_usd FROM trades
            WHERE bot_id='structure' AND mode='paper'
              AND fill_status='closed' AND entry_at >= :ws
              AND pnl_usd IS NOT NULL
            """
        ), {"ws": window_start}).all()
        pnls = [float(r.pnl_usd) for r in rows]
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        wr = (wins / n) if n > 0 else 0.0
        net_pnl = sum(pnls)
        net_pnl_pct = (net_pnl / paper_capital * 100.0) if paper_capital > 0 else 0.0
        sharpe = _sharpe(pnls)

        # Signal rate per week — last 6 weeks
        weekly_rows = s.execute(text(
            """
            SELECT date_trunc('week', created_at) AS wk, COUNT(*) AS cnt
            FROM signals
            WHERE bot_id='structure' AND created_at >= :ws
            GROUP BY 1 ORDER BY 1 DESC LIMIT 6
            """
        ), {"ws": window_start}).all()
        weekly_counts = [int(r.cnt) for r in weekly_rows]
        # consecutive quiet weeks (count from most recent backwards)
        quiet_threshold = STRUCTURE_CRITERIA["signal_rate_min_per_week"]
        consecutive_quiet = 0
        for cnt in weekly_counts:
            if cnt < quiet_threshold:
                consecutive_quiet += 1
            else:
                break
        avg_rate = (sum(weekly_counts) / len(weekly_counts)) if weekly_counts else 0.0

        # Slippage — Build B shadow trades only
        slip_row = s.execute(text(
            """
            SELECT
              AVG((sim_metadata->>'realized_slippage_bps')::float
                  / NULLIF((sim_metadata->>'modeled_slippage_bps')::float, 0)) AS ratio,
              AVG((sim_metadata->>'realized_slippage_bps')::float) AS abs_bps,
              COUNT(*) AS n
            FROM trades
            WHERE bot_id='structure' AND mode='shadow' AND fill_status='closed'
              AND entry_at >= :ws
              AND sim_metadata->>'realized_slippage_bps' IS NOT NULL
            """
        ), {"ws": window_start}).first()
        slippage_ratio = float(slip_row.ratio) if slip_row and slip_row.ratio else None
        slippage_abs = float(slip_row.abs_bps) if slip_row and slip_row.abs_bps else None
        slippage_n = int(slip_row.n) if slip_row and slip_row.n else 0

    # Evaluate kill criteria
    kill_triggers: list[str] = []
    warning_triggers: list[str] = []

    # A. WR floor (gated on N)
    if n >= STRUCTURE_CRITERIA["wr_min_n"]:
        if wr < STRUCTURE_CRITERIA["wr_floor"]:
            kill_triggers.append("structure_wr_below_floor")
        elif wr < STRUCTURE_CRITERIA["wr_floor"] * (1 + WARNING_MARGIN):
            warning_triggers.append("structure_wr_within_margin")

    # B. Signal rate quiet weeks
    if consecutive_quiet >= STRUCTURE_CRITERIA["signal_rate_quiet_weeks_threshold"]:
        kill_triggers.append("structure_signal_rate_quiet")
    elif consecutive_quiet >= STRUCTURE_CRITERIA["signal_rate_quiet_weeks_threshold"] - 1:
        warning_triggers.append("structure_signal_rate_within_margin")

    # C. Slippage (only meaningful with shadow data)
    if slippage_n >= 10:  # need a minimum sample to evaluate slippage
        if (slippage_ratio is not None and slippage_ratio > STRUCTURE_CRITERIA["slippage_ratio_max"]) \
           or (slippage_abs is not None and slippage_abs > STRUCTURE_CRITERIA["slippage_absolute_max_bps"]):
            kill_triggers.append("structure_slippage_exceeds_threshold")

    # Promotion eligibility
    promo = PROMOTION_CRITERIA["structure"]
    promote_eligible = (
        n >= STRUCTURE_CRITERIA["wr_min_n"]
        and wr >= promo["wr"]
        and net_pnl_pct >= promo["pnl_pct"]
        and (sharpe is not None and sharpe >= promo["sharpe"])
        and (slippage_ratio is None or slippage_ratio <= promo["slippage_ratio_max"])
    )

    return {
        "bot_id": "structure",
        "window": _window_metadata(),
        "paper_capital_usd": paper_capital,
        "n": n,
        "wr": round(wr, 4),
        "net_pnl_usd": round(net_pnl, 2),
        "net_pnl_pct": round(net_pnl_pct, 3),
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "signal_rate_per_week_avg": round(avg_rate, 2),
        "consecutive_quiet_weeks": consecutive_quiet,
        "slippage_ratio": round(slippage_ratio, 3) if slippage_ratio is not None else None,
        "slippage_abs_bps": round(slippage_abs, 2) if slippage_abs is not None else None,
        "slippage_n": slippage_n,
        "kill_triggers": kill_triggers,
        "warning_triggers": warning_triggers,
        "promote_eligible": promote_eligible,
        "last_checked_at": _now().isoformat(),
    }


def _compute_copy_status() -> dict[str, Any]:
    paper_capital = _paper_capital_for("copy")
    window_start = WINDOW_START
    tier = COPY_CRITERIA["wallet_tier_filter"]

    with session_scope() as s:
        # Active-tier closed trades only — that's the validation set
        rows = s.execute(text(
            """
            SELECT pnl_usd FROM trades
            WHERE bot_id='copy' AND mode='paper'
              AND fill_status='closed' AND entry_at >= :ws
              AND pnl_usd IS NOT NULL
              AND sim_metadata->>'wallet_tier' = :tier
            """
        ), {"ws": window_start, "tier": tier}).all()
        pnls = [float(r.pnl_usd) for r in rows]
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        wr = (wins / n) if n > 0 else 0.0
        net_pnl = sum(pnls)
        net_pnl_pct = (net_pnl / paper_capital * 100.0) if paper_capital > 0 else 0.0
        sharpe = _sharpe(pnls)

    # Evaluate kill criteria
    kill_triggers: list[str] = []
    warning_triggers: list[str] = []

    if n >= COPY_CRITERIA["wr_min_n"]:
        if wr < COPY_CRITERIA["wr_floor"]:
            kill_triggers.append("copy_wr_below_floor")
        elif wr < COPY_CRITERIA["wr_floor"] * (1 + WARNING_MARGIN):
            warning_triggers.append("copy_wr_within_margin")

        if net_pnl_pct < COPY_CRITERIA["pnl_floor_pct"]:
            kill_triggers.append("copy_pnl_below_floor")
        elif net_pnl_pct < COPY_CRITERIA["pnl_floor_pct"] * (1 + WARNING_MARGIN):
            warning_triggers.append("copy_pnl_within_margin")

    # Promotion eligibility
    promo = PROMOTION_CRITERIA["copy"]
    promote_eligible = (
        n >= COPY_CRITERIA["wr_min_n"]
        and wr >= promo["wr"]
        and net_pnl_pct >= promo["pnl_pct"]
        and (sharpe is not None and sharpe >= promo["sharpe"])
    )

    return {
        "bot_id": "copy",
        "window": _window_metadata(),
        "paper_capital_usd": paper_capital,
        "wallet_tier_filter": tier,
        "n": n,
        "wr": round(wr, 4),
        "net_pnl_usd": round(net_pnl, 2),
        "net_pnl_pct": round(net_pnl_pct, 3),
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "kill_triggers": kill_triggers,
        "warning_triggers": warning_triggers,
        "promote_eligible": promote_eligible,
        "last_checked_at": _now().isoformat(),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_COMPUTERS = {
    "structure": _compute_structure_status,
    "copy": _compute_copy_status,
}


def _prior_status(bot_id: str) -> Optional[dict[str, Any]]:
    """Read previous kill_criteria_status from DB for transition detection."""
    with session_scope() as s:
        row = s.query(BotState).filter(BotState.bot_id == bot_id).one_or_none()
        if row is None or row.kill_criteria_status is None:
            return None
        return row.kill_criteria_status if isinstance(row.kill_criteria_status, dict) else None


def _write_status(bot_id: str, status: dict[str, Any]) -> None:
    with session_scope() as s:
        row = s.query(BotState).filter(BotState.bot_id == bot_id).one_or_none()
        if row is None:
            log.warning("kill_criteria_no_bot_state_row", bot_id=bot_id)
            return
        row.kill_criteria_status = status


def _emit_transition_alerts(bot_id: str, prior: Optional[dict], current: dict) -> None:
    """Emit P1 alerts only when triggers TRANSITION from not-fired to fired."""
    prior_kill = set(prior.get("kill_triggers", []) if prior else [])
    prior_warn = set(prior.get("warning_triggers", []) if prior else [])
    curr_kill = set(current.get("kill_triggers", []))
    curr_warn = set(current.get("warning_triggers", []))

    newly_killed = curr_kill - prior_kill
    newly_warned = curr_warn - prior_warn - curr_kill  # don't double-alert if both fired

    for trigger in newly_killed:
        emit_alert(
            severity=Severity.P1,
            title=f"[{bot_id}] kill criterion fired: {trigger}",
            body=(
                f"Criterion `{trigger}` has fired for {bot_id}.\n"
                f"Window day: {current['window']['day_of_window']}/60 (primary).\n"
                f"Current state: WR={current.get('wr'):.3f} N={current.get('n')} "
                f"net_pnl_pct={current.get('net_pnl_pct'):.2f}%.\n\n"
                "This is an ALERT, not an auto-halt. Per the 2026-05-25 decision "
                "log entry, Roy retains manual judgment on actual kill/promote. "
                "Review the data, decide, and (if killing) run halt_bot() manually."
            ),
            bot_id=bot_id,
            event_type="kill_criterion_fired",
            metadata={"trigger": trigger, "snapshot": current},
        )
        write_audit("kill_criterion_fired", bot_id=bot_id, payload={"trigger": trigger, "snapshot": current})

    for trigger in newly_warned:
        emit_alert(
            severity=Severity.P2,
            title=f"[{bot_id}] kill criterion within warning margin: {trigger}",
            body=(
                f"Criterion `{trigger}` is within 10% of triggering for {bot_id}.\n"
                f"Window day: {current['window']['day_of_window']}/60.\n"
                f"Snapshot: WR={current.get('wr'):.3f} N={current.get('n')} "
                f"net_pnl_pct={current.get('net_pnl_pct'):.2f}%."
            ),
            bot_id=bot_id,
            event_type="kill_criterion_warning_margin",
            metadata={"trigger": trigger, "snapshot": current},
        )

    # Promotion transition
    prior_promote = prior.get("promote_eligible", False) if prior else False
    if current.get("promote_eligible") and not prior_promote:
        emit_alert(
            severity=Severity.P1,
            title=f"[{bot_id}] PROMOTION criteria met",
            body=(
                f"{bot_id} now meets all promotion criteria for Phase L1 live deployment.\n"
                f"WR={current.get('wr'):.3f} (>=0.55), "
                f"net_pnl_pct={current.get('net_pnl_pct'):.2f}% (>=5.0%), "
                f"sharpe={current.get('sharpe')} (>=1.0).\n\n"
                "Per the 2026-05-25 decision log, this is an alert, not an "
                "auto-promotion. Review and decide."
            ),
            bot_id=bot_id,
            event_type="promotion_criteria_met",
            metadata={"snapshot": current},
        )
        write_audit("promotion_criteria_met", bot_id=bot_id, payload={"snapshot": current})


def check_all_bots_kill_criteria() -> None:
    """Compute, persist, and (on transition only) alert.

    Called by scoring engine cron every 60 min.
    """
    for bot_id, compute in _COMPUTERS.items():
        try:
            prior = _prior_status(bot_id)
            status = compute()
            _write_status(bot_id, status)
            _emit_transition_alerts(bot_id, prior, status)
        except Exception:
            log.exception("kill_criteria_check_failed", bot_id=bot_id)
