"""STRUCTURE position reconciliation.

Compares the bot's internal open-positions view (from `trades` table where
fill_status='open' and bot_id='structure') against Hyperliquid's actual
user_state for the master wallet.

We reconcile by **signed native token quantity**, not USD notional. Comparing
USD always shows phantom drift as price moves between fill and reconcile time
(e.g., bot snapshots size_usd=11.00 at fill; HL reports notional=11.07 at
reconcile because the mark moved 0.6% — same 12 tokens both sides). Native
quantity is conserved through price changes, so any nonzero drift indicates
a real position discrepancy.

Drift > threshold (configured in framework/reconciliation.py) → halt + P1.

Phantom-drift auto-recovery (added 2026-05-29): the bookkeeping-bug class
from 2026-05-28 (shadow trade closed cleanly on venue but bot's trades row
stayed fill_status='open') was recurring even after stale_position_cleanup
was shipped — the cleanup only triggers after 2x timeout_hours but the
drift halt fires on the next reconciliation cycle (every 5 min) so the
bot would halt long before cleanup could help. The pattern (venue=0,
bot!=0, drift=100%) is now auto-recovered: force-close the phantom open
trades for that asset, emit P2 alert, and skip emitting the snapshot
(no halt). Genuine mismatches (where venue HAS a nonzero position
diverging from bot) still halt as before.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from sqlalchemy import select

from bots.structure.venue import HyperliquidVenue
from framework.alerts import emit_alert
from framework.audit import write_audit
from framework.db import session_scope
from framework.logging_setup import get_logger
from framework.models import Trade
from framework.reconciliation import PositionSnapshot
from monitoring.alerting.taxonomy import Severity

log = get_logger(__name__)

BOT_ID = "structure"
EPS = 1e-9


def _force_close_phantom_trades(asset: str, venue: str) -> int:
    """Force-close open shadow/live trades for an asset where venue reports
    zero position. Returns count of trades closed.

    Used by the fetcher's phantom-drift auto-recovery. Sets exit_reason
    to 'phantom_drift_recovery' so these can be audited later.
    """
    now = datetime.now(timezone.utc)
    closed = 0
    closed_ids: list[int] = []
    with session_scope() as s:
        q = select(Trade).where(
            Trade.bot_id == BOT_ID,
            Trade.asset == asset,
            Trade.venue == venue,
            Trade.fill_status == "open",
            Trade.mode.in_(("shadow", "live")),
        )
        for trade in s.execute(q).scalars():
            trade.fill_status = "closed"
            trade.exit_reason = "phantom_drift_recovery"
            trade.exit_at = now
            trade.exit_price = trade.entry_price
            trade.pnl_usd = 0.0
            trade.pnl_pct = 0.0
            closed += 1
            closed_ids.append(trade.id)
    if closed > 0:
        try:
            write_audit(
                "phantom_drift_recovered",
                bot_id=BOT_ID,
                payload={"asset": asset, "venue": venue,
                         "trade_ids": closed_ids, "n_closed": closed},
            )
        except Exception:
            log.exception("phantom_drift_audit_write_failed")
        try:
            emit_alert(
                severity=Severity.P2,
                title=f"[structure] phantom drift on {venue}/{asset} auto-recovered ({closed} trade(s))",
                body=(
                    f"Reconciliation detected venue position = 0 but bot's open "
                    f"trades claimed a nonzero position on {venue}/{asset} "
                    f"(the post-close-residual bookkeeping bug). "
                    f"Force-closed {closed} trade(s) (ids: {closed_ids}) with "
                    f"exit_reason='phantom_drift_recovery', PnL=0. No halt fired.\n\n"
                    f"This is the auto-recovery path shipped 2026-05-29. If you "
                    f"see this alert >2x per day for the same asset, investigate "
                    f"the close-path code that's missing the DB update."
                ),
                bot_id=BOT_ID,
                event_type="phantom_drift_recovered",
                metadata={"asset": asset, "venue": venue, "n_closed": closed},
            )
        except Exception:
            log.exception("phantom_drift_alert_emit_failed")
    return closed


def _bot_open_positions() -> dict[tuple[str, str], float]:
    """Return {(venue, asset): bot_tracked_size_native_signed}.

    Native (token) quantity = size_usd / entry_price, signed by direction.
    PAPER trades are excluded by design — they have no on-venue counterpart
    and would always show 100% drift, which is non-actionable noise.
    """
    out: dict[tuple[str, str], float] = {}
    with session_scope() as s:
        q = select(Trade).where(
            Trade.bot_id == BOT_ID,
            Trade.fill_status == "open",
            Trade.mode.in_(("shadow", "live")),
        )
        for trade in s.execute(q).scalars():
            if trade.size_usd is None or trade.entry_price is None or trade.entry_price <= 0:
                continue
            size_native = float(trade.size_usd) / float(trade.entry_price)
            sign = 1.0 if trade.direction == "long" else -1.0
            key = (trade.venue, trade.asset)
            out[key] = out.get(key, 0.0) + (size_native * sign)
    return out


def fetch_hyperliquid_positions(venue: HyperliquidVenue) -> dict[tuple[str, str], float]:
    """Return {(venue, asset): hyperliquid_actual_size_native_signed} for the master wallet.

    Raises on transport failure (HL 502, timeout, connection reset, etc.) —
    DO NOT swallow into an empty dict. An empty dict means "venue has zero
    positions everywhere," which the caller will then compare against the
    bot's real tracked positions, producing a phantom 100% drift halt.
    `framework.reconciliation.reconcile_once` already catches fetcher
    exceptions, logs `reconcile_fetcher_error`, and skips this cycle —
    that is the correct behavior for transient venue-API failures.
    """
    out: dict[tuple[str, str], float] = {}
    settings = venue.settings
    if not settings.hyperliquid_master_address:
        return out
    positions = venue.user_positions(settings.hyperliquid_master_address)
    for p in positions:
        key = ("hyperliquid", p.asset)
        out[key] = out.get(key, 0.0) + p.size_native
    return out


def make_fetcher(venue: HyperliquidVenue):
    """Return a closure suitable for register_venue_fetcher('hyperliquid', fn)."""

    def fetcher() -> list[PositionSnapshot]:
        bot = _bot_open_positions()
        venue_actual = fetch_hyperliquid_positions(venue)

        keys = set(bot.keys()) | set(venue_actual.keys())
        snapshots: list[PositionSnapshot] = []
        for (vn, asset) in keys:
            bot_size = bot.get((vn, asset), 0.0)
            ven_size = venue_actual.get((vn, asset), 0.0)
            if abs(bot_size) < EPS and abs(ven_size) < EPS:
                continue
            denom = max(abs(ven_size), abs(bot_size), EPS)
            drift_pct = abs(bot_size - ven_size) / denom * 100.0

            # Phantom-drift auto-recovery (2026-05-29): venue=0, bot!=0,
            # drift=100% is the post-close-residual bookkeeping bug.
            # Force-close the orphaned trades and don't emit the snapshot
            # (so no halt fires). Genuine mismatches where the venue HAS
            # a nonzero position diverging from the bot still emit and halt.
            if abs(ven_size) < EPS and abs(bot_size) >= EPS and drift_pct >= 99.0:
                closed = _force_close_phantom_trades(asset, vn)
                if closed > 0:
                    log.warning(
                        "phantom_drift_auto_recovered",
                        asset=asset, venue=vn,
                        bot_size=bot_size, venue_size=ven_size,
                        n_closed=closed,
                    )
                    continue

            snapshots.append(PositionSnapshot(
                bot_id=BOT_ID,
                asset=asset,
                venue=vn,
                bot_size=bot_size,
                venue_size=ven_size,
                drift_pct=drift_pct,
            ))
        return snapshots

    return fetcher
