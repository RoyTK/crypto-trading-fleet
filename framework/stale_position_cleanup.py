"""Stale shadow/live position auto-close.

Bug context (2026-05-28 STRUCTURE drift halt on VVV): the bot's shadow
trade closed cleanly on Hyperliquid (real position = 0), but the bot's
internal trades-table row stayed fill_status='open' due to a missed DB
update path. Reconciliation then saw bot_size=-0.66 vs venue_size=0.0
and halted on phantom drift.

This monitor runs every 30 minutes (alongside dd_monitor) and:
1. Queries shadow/live trades with fill_status='open' and entry_at
   older than `STALE_HOURS_MULTIPLIER × timeout_hours`.
2. For each stale trade, force-closes it with exit_reason='stale_force_close',
   exit_price set to last-known entry_price (so PnL is logged as 0),
   and emits a P2 alert so the operator knows.
3. Audit-logs the event for future debug.

Conservative — does not close paper trades (those never had a venue
counterpart), only shadow/live trades where the DB row truly diverged
from reality. Triggers at 2× timeout_hours so we don't race normal
close-paths during volatile periods.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select

from framework.alerts import emit_alert
from framework.audit import write_audit
from framework.db import session_scope
from framework.logging_setup import get_logger
from framework.models import Trade
from monitoring.alerting.taxonomy import Severity


log = get_logger(__name__)


# Trade is "stale" if it's been open for STALE_MULTIPLIER × timeout_hours
# without being marked closed. Default: 2× the signal's stated timeout.
STALE_MULTIPLIER = 2.0

# Default timeout when sim_metadata doesn't carry it (defensive)
DEFAULT_TIMEOUT_HOURS = 12.0


def _timeout_hours_for(trade: Trade) -> float:
    """Read sim_metadata['timeout_hours'] when present; else fall back."""
    md = trade.sim_metadata or {}
    if isinstance(md, dict):
        raw = md.get("timeout_hours")
        try:
            if raw is not None:
                return float(raw)
        except (TypeError, ValueError):
            pass
    return DEFAULT_TIMEOUT_HOURS


def check_and_close_stale_positions() -> int:
    """Find + force-close stale open shadow/live trades. Returns count closed.

    Called every 30 min by the scoring engine cron.
    """
    now = datetime.now(timezone.utc)
    closed = 0
    with session_scope() as s:
        q = select(Trade).where(
            Trade.fill_status == "open",
            Trade.mode.in_(("shadow", "live")),
            Trade.entry_at.isnot(None),
        )
        candidates = list(s.execute(q).scalars())
        for trade in candidates:
            if trade.entry_at is None:
                continue
            timeout_h = _timeout_hours_for(trade)
            stale_threshold = trade.entry_at + timedelta(hours=timeout_h * STALE_MULTIPLIER)
            if now < stale_threshold:
                continue

            # Stale — force-close with marker exit
            trade.fill_status = "closed"
            trade.exit_reason = "stale_force_close"
            trade.exit_at = now
            # Use entry_price as exit_price so PnL = 0 (we don't know real outcome)
            trade.exit_price = trade.entry_price
            trade.pnl_usd = 0.0
            trade.pnl_pct = 0.0
            closed += 1

            try:
                write_audit(
                    "stale_position_force_closed",
                    bot_id=trade.bot_id,
                    payload={
                        "trade_id": trade.id,
                        "asset": trade.asset,
                        "venue": trade.venue,
                        "mode": trade.mode,
                        "direction": trade.direction,
                        "size_usd": float(trade.size_usd) if trade.size_usd else None,
                        "entry_at": trade.entry_at.isoformat(),
                        "stale_threshold_hours": timeout_h * STALE_MULTIPLIER,
                    },
                )
            except Exception:
                log.exception("stale_audit_write_failed", trade_id=trade.id)

            try:
                emit_alert(
                    severity=Severity.P2,
                    title=f"[{trade.bot_id}] stale shadow/live position force-closed",
                    body=(
                        f"Trade {trade.id} ({trade.mode}/{trade.asset}/{trade.direction}) was "
                        f"still open {(now - trade.entry_at).total_seconds() / 3600.0:.1f}h "
                        f"after entry, exceeding the {timeout_h * STALE_MULTIPLIER:.1f}h stale "
                        f"threshold.\n\n"
                        f"Force-closed with exit_reason='stale_force_close', PnL=0.\n"
                        f"Likely cause: bot crashed or missed the DB-update path between "
                        f"venue close and trades row update. Investigate logs around "
                        f"{trade.entry_at.isoformat()} → now."
                    ),
                    bot_id=trade.bot_id,
                    event_type="stale_position_force_closed",
                    metadata={
                        "trade_id": trade.id,
                        "asset": trade.asset,
                        "mode": trade.mode,
                    },
                )
            except Exception:
                log.exception("stale_alert_emit_failed", trade_id=trade.id)

    if closed > 0:
        log.warning("stale_positions_closed", count=closed)
    return closed
