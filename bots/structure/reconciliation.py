"""STRUCTURE position reconciliation.

Compares the bot's internal open-positions view (from `trades` table where
fill_status='open' and bot_id='structure') against Hyperliquid's actual
user_state for the master wallet. Drift > 0.5% → halt + P1 alert (handled by
framework/reconciliation.py via the registered fetcher).

Build A (no agent key): we still query user_state read-only; that just shows
no positions (or the master's manual positions, which the bot should ignore
since they're not bot-tracked). The reconciliation logic stays correct in
both build phases.
"""
from __future__ import annotations

from typing import Any
from sqlalchemy import select

from bots.structure.venue import HyperliquidVenue
from framework.db import session_scope
from framework.logging_setup import get_logger
from framework.models import Trade
from framework.reconciliation import PositionSnapshot

log = get_logger(__name__)

BOT_ID = "structure"
EPS = 1e-9


def _bot_open_positions() -> dict[tuple[str, str], float]:
    """Return {(venue, asset): bot_tracked_size_usd_signed}.

    Sums open Trades by (venue, asset) using signed notional (+long, -short).
    """
    out: dict[tuple[str, str], float] = {}
    with session_scope() as s:
        q = select(Trade).where(
            Trade.bot_id == BOT_ID,
            Trade.fill_status == "open",
        )
        for trade in s.execute(q).scalars():
            if trade.size_usd is None:
                continue
            sign = 1.0 if trade.direction == "long" else -1.0
            key = (trade.venue, trade.asset)
            out[key] = out.get(key, 0.0) + (trade.size_usd * sign)
    return out


def fetch_hyperliquid_positions(venue: HyperliquidVenue) -> dict[tuple[str, str], float]:
    """Return {(venue, asset): hyperliquid_actual_size_usd_signed} for the master wallet."""
    out: dict[tuple[str, str], float] = {}
    settings = venue.settings
    if not settings.hyperliquid_master_address:
        return out
    try:
        positions = venue.user_positions(settings.hyperliquid_master_address)
    except Exception:
        log.exception("user_positions_fetch_failed")
        return out
    for p in positions:
        key = ("hyperliquid", p.asset)
        out[key] = out.get(key, 0.0) + p.notional_usd
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
