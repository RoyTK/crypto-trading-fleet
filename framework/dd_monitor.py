"""Drawdown halt monitor.

Watches a bot's closed paper-trade PnL across trailing windows (24h / 7d /
all-time) and halts the bot if it crosses a per-bot drawdown threshold.

Called periodically by the scoring engine (every 15 min). Halts produced
here go through `framework.halt_state.halt_bot()` so they integrate with
`is_bot_halted()` checks the bot loop already does.

Per-bot thresholds are passed in by the caller (different bots have different
DD discipline per the design — STRUCTURE 15/30/45, COPY 12/28/50, etc.).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, func

from framework.alerts import emit_alert
from framework.audit import write_audit
from framework.db import session_scope
from framework.halt_state import halt_bot, is_bot_halted
from framework.logging_setup import get_logger
from framework.models import Trade
from monitoring.alerting.taxonomy import Severity


log = get_logger(__name__)


def _trailing_paper_pnl_usd(bot_id: str, hours: Optional[int] = None) -> float:
    """Sum of pnl_usd for closed paper trades in the trailing window.

    hours=None → all-time cumulative.
    """
    with session_scope() as s:
        q = select(func.coalesce(func.sum(Trade.pnl_usd), 0.0)).where(
            Trade.bot_id == bot_id,
            Trade.mode == "paper",
            Trade.fill_status == "closed",
        )
        if hours is not None:
            since = datetime.now(timezone.utc) - timedelta(hours=hours)
            q = q.where(Trade.exit_at >= since)
        return float(s.execute(q).scalar() or 0.0)


def check_dd_breaches(
    bot_id: str,
    paper_capital_usd: float,
    daily_pct: float,
    weekly_pct: float,
    total_pct: float,
) -> Optional[str]:
    """Check trailing PnL windows; halt bot if any DD threshold breached.

    Returns the halt_type that fired ('dd_daily' | 'dd_weekly' | 'dd_total'),
    or None if no breach. Returns None if bot is already halted (won't double-halt).
    """
    if paper_capital_usd <= 0:
        return None
    if is_bot_halted(bot_id):
        return None

    # Total is checked first — most severe, longest halt
    total_pnl = _trailing_paper_pnl_usd(bot_id, hours=None)
    total_pnl_pct = (total_pnl / paper_capital_usd) * 100.0
    if total_pnl_pct <= -total_pct:
        halt_bot(
            bot_id,
            halt_type="dd_total",
            reason=f"cumulative paper PnL {total_pnl_pct:.2f}% breached -{total_pct}% threshold (cum_pnl=${total_pnl:.2f} / capital=${paper_capital_usd:.2f})",
            severity="p0",
            metadata={"pnl_usd": total_pnl, "pnl_pct": total_pnl_pct, "threshold_pct": total_pct, "window": "total"},
        )
        emit_alert(
            severity=Severity.P0,
            title=f"[{bot_id}] TOTAL DD halt — manual review required",
            body=f"Cumulative paper PnL {total_pnl_pct:.2f}% (${total_pnl:.2f}) breached the -{total_pct}% total-DD threshold. Bot halted indefinitely until manual review.",
            bot_id=bot_id,
            event_type="dd_halt_total",
            metadata={"pnl_usd": total_pnl, "pnl_pct": total_pnl_pct, "threshold_pct": total_pct},
        )
        return "dd_total"

    weekly_pnl = _trailing_paper_pnl_usd(bot_id, hours=24 * 7)
    weekly_pnl_pct = (weekly_pnl / paper_capital_usd) * 100.0
    if weekly_pnl_pct <= -weekly_pct:
        until = datetime.now(timezone.utc) + timedelta(days=7)
        halt_bot(
            bot_id,
            halt_type="dd_weekly",
            reason=f"trailing 7d paper PnL {weekly_pnl_pct:.2f}% breached -{weekly_pct}% threshold",
            severity="p1",
            halted_until=until,
            metadata={"pnl_usd": weekly_pnl, "pnl_pct": weekly_pnl_pct, "threshold_pct": weekly_pct, "window": "7d", "halted_until": until.isoformat()},
        )
        emit_alert(
            severity=Severity.P1,
            title=f"[{bot_id}] weekly DD halt — 7d auto-resume",
            body=f"Trailing 7d paper PnL {weekly_pnl_pct:.2f}% (${weekly_pnl:.2f}) breached the -{weekly_pct}% weekly-DD threshold. Bot halted until {until.isoformat()}.",
            bot_id=bot_id,
            event_type="dd_halt_weekly",
        )
        return "dd_weekly"

    daily_pnl = _trailing_paper_pnl_usd(bot_id, hours=24)
    daily_pnl_pct = (daily_pnl / paper_capital_usd) * 100.0
    if daily_pnl_pct <= -daily_pct:
        until = datetime.now(timezone.utc) + timedelta(hours=24)
        halt_bot(
            bot_id,
            halt_type="dd_daily",
            reason=f"trailing 24h paper PnL {daily_pnl_pct:.2f}% breached -{daily_pct}% threshold",
            severity="p1",
            halted_until=until,
            metadata={"pnl_usd": daily_pnl, "pnl_pct": daily_pnl_pct, "threshold_pct": daily_pct, "window": "24h", "halted_until": until.isoformat()},
        )
        emit_alert(
            severity=Severity.P1,
            title=f"[{bot_id}] daily DD halt — 24h auto-resume",
            body=f"Trailing 24h paper PnL {daily_pnl_pct:.2f}% (${daily_pnl:.2f}) breached the -{daily_pct}% daily-DD threshold. Bot halted until {until.isoformat()}.",
            bot_id=bot_id,
            event_type="dd_halt_daily",
        )
        return "dd_daily"

    return None


# Per-bot threshold registry. Locked Item #3 design.
BOT_DD_THRESHOLDS: dict[str, dict[str, float]] = {
    "structure": {"daily": 15.0, "weekly": 30.0, "total": 45.0},
    "copy": {"daily": 12.0, "weekly": 28.0, "total": 50.0},
    "event": {"daily": 12.0, "weekly": 28.0, "total": 50.0},
    "sniper": {"daily": 8.0, "weekly": 20.0, "total": 40.0},
}

# Per-bot paper capital — read from each bot's settings module.
# Hardcoded fallback only if the bot's settings import fails (defensive).
_PAPER_CAPITAL_FALLBACK = 1000.0


def _paper_capital_for(bot_id: str) -> float:
    """Read paper_capital_usd from env vars directly.

    NOTE 2026-05-25: do NOT import from bots.*.config here. The scoring
    container only mounts ./framework — `bots/` is not on its path, so the
    import would ModuleNotFoundError and we'd silently fall back to the
    default. (This bug was present from 2026-05-24 commit 1dac39d through
    2026-05-25 when the kill_criteria_monitor exposed it.) Env vars are
    loaded into every container via .env, so this is reliable.
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
                log.warning("paper_capital_invalid", bot_id=bot_id, raw=raw)
    return _PAPER_CAPITAL_FALLBACK


def check_all_bots_dd() -> None:
    """Run check_dd_breaches for every bot using its configured thresholds.
    Called by the scoring engine cron.
    """
    for bot_id, th in BOT_DD_THRESHOLDS.items():
        try:
            paper_cap = _paper_capital_for(bot_id)
            result = check_dd_breaches(
                bot_id=bot_id,
                paper_capital_usd=paper_cap,
                daily_pct=th["daily"],
                weekly_pct=th["weekly"],
                total_pct=th["total"],
            )
            if result:
                write_audit(
                    "dd_breach_detected",
                    bot_id=bot_id,
                    payload={"halt_type": result, "thresholds": th, "paper_capital_usd": paper_cap},
                )
        except Exception:
            log.exception("dd_check_failed", bot_id=bot_id)
