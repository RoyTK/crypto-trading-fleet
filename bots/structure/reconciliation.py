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
