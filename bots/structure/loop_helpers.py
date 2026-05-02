"""Persistence helpers for the STRUCTURE main loop.

Wraps Signal + Trade table writes so the main loop stays focused on
orchestration. Bots write to `signals` and `trades` only — never `scores`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from bots.base.fill_simulator_base import SimulatedFill
from bots.structure.signals.base import SignalCandidate
from framework.audit import write_audit
from framework.db import session_scope
from framework.models import Signal, Trade


BOT_ID = "structure"


@dataclass
class OpenPaperTrade:
    """In-memory view of a single open paper Trade row."""
    trade_id: int
    signal_id: int
    asset: str
    venue: str
    direction: str
    entry_price: float
    size_usd: float
    leverage: float
    entry_at: datetime
    stop_pct: Optional[float]
    take_profit_pct: Optional[float]
    timeout_hours: Optional[int]


def persist_signal(candidate: SignalCandidate) -> int:
    with session_scope() as s:
        sig = Signal(
            bot_id=BOT_ID,
            signal_type=candidate.signal_type,
            asset=candidate.asset,
            venue=candidate.venue,
            direction=candidate.direction,
            payload=candidate.payload,
        )
        s.add(sig)
        s.flush()
        return sig.id


def persist_paper_trade(
    *,
    signal_id: int,
    candidate: SignalCandidate,
    sim_fill: SimulatedFill,
    notional_usd: float,
    leverage: float,
) -> Optional[int]:
    """Insert a paper Trade. If sim_fill is no-fill, write a no_fill row and
    return its id but the caller will treat it as not-open.
    """
    with session_scope() as s:
        trade = Trade(
            bot_id=BOT_ID,
            signal_id=signal_id,
            mode="paper",
            asset=candidate.asset,
            venue=candidate.venue,
            direction=candidate.direction,
            entry_price=sim_fill.fill_price,
            size_usd=notional_usd if sim_fill.fill_price is not None else None,
            leverage=leverage,
            entry_at=datetime.now(timezone.utc) if sim_fill.fill_price is not None else None,
            fees_usd=sim_fill.fees_usd,
            fill_status="open" if sim_fill.fill_price is not None else "no_fill",
            sim_metadata={
                "slippage_bps": sim_fill.slippage_bps,
                "no_fill_reason": sim_fill.no_fill_reason,
                "fill_metadata": sim_fill.metadata,
                "stop_pct": candidate.stop_pct,
                "take_profit_pct": candidate.take_profit_pct,
                "timeout_hours": candidate.timeout_hours,
            },
        )
        s.add(trade)
        s.flush()
        trade_id = trade.id
    write_audit(
        "paper_trade_opened" if sim_fill.fill_price is not None else "paper_trade_no_fill",
        bot_id=BOT_ID,
        payload={
            "trade_id": trade_id,
            "signal_id": signal_id,
            "asset": candidate.asset,
            "direction": candidate.direction,
            "size_usd": notional_usd,
            "leverage": leverage,
        },
    )
    return trade_id


def list_open_paper_trades() -> list[OpenPaperTrade]:
    out: list[OpenPaperTrade] = []
    with session_scope() as s:
        q = select(Trade).where(
            Trade.bot_id == BOT_ID,
            Trade.mode == "paper",
            Trade.fill_status == "open",
        )
        for t in s.execute(q).scalars():
            md = t.sim_metadata or {}
            out.append(OpenPaperTrade(
                trade_id=t.id,
                signal_id=t.signal_id or 0,
                asset=t.asset,
                venue=t.venue,
                direction=t.direction,
                entry_price=float(t.entry_price or 0.0),
                size_usd=float(t.size_usd or 0.0),
                leverage=float(t.leverage or 1.0),
                entry_at=t.entry_at or t.created_at,
                stop_pct=md.get("stop_pct"),
                take_profit_pct=md.get("take_profit_pct"),
                timeout_hours=md.get("timeout_hours"),
            ))
    return out


def close_paper_trade(
    *,
    trade_id: int,
    exit_price: float,
    exit_fill: SimulatedFill,
    exit_reason: str,
) -> None:
    """Update the existing paper Trade row in place — we don't insert a new row."""
    with session_scope() as s:
        t = s.get(Trade, trade_id)
        if t is None or t.fill_status != "open":
            return
        t.exit_price = exit_price
        t.exit_at = datetime.now(timezone.utc)
        t.exit_reason = exit_reason
        t.fill_status = "closed"
        t.fees_usd = float(t.fees_usd or 0.0) + exit_fill.fees_usd

        # PnL math: percent move adjusted for direction × leverage; signed
        if t.entry_price and t.entry_price > 0:
            raw_pct = (exit_price - t.entry_price) / t.entry_price * 100.0
            if t.direction == "short":
                raw_pct = -raw_pct
            t.pnl_pct = raw_pct * float(t.leverage or 1.0)
            t.pnl_usd = float(t.size_usd or 0.0) * (t.pnl_pct / 100.0)
        md = dict(t.sim_metadata or {})
        md["exit_slippage_bps"] = exit_fill.slippage_bps
        t.sim_metadata = md
    write_audit(
        "paper_trade_closed",
        bot_id=BOT_ID,
        payload={
            "trade_id": trade_id,
            "exit_reason": exit_reason,
            "exit_price": exit_price,
        },
    )


def panic_close_all_open() -> int:
    """Mark every open paper Trade as closed with exit_reason='panic'.

    Returns count closed. Used by on_panic() — we don't bother computing PnL
    here since /panic is operationally driven, not a market signal.
    """
    n = 0
    with session_scope() as s:
        q = select(Trade).where(
            Trade.bot_id == BOT_ID,
            Trade.mode == "paper",
            Trade.fill_status == "open",
        )
        for t in s.execute(q).scalars():
            t.fill_status = "closed"
            t.exit_reason = "panic"
            t.exit_at = datetime.now(timezone.utc)
            n += 1
    if n > 0:
        write_audit(
            "panic_paper_trades_closed",
            bot_id=BOT_ID,
            payload={"count": n},
        )
    return n


def find_open_shadow_for_paper(paper_trade_id: int) -> Optional[int]:
    """Look up the shadow Trade paired with a paper Trade via calibration_records.

    Returns the shadow_trade_id if there's an open shadow trade paired with
    this paper trade, else None.
    """
    from framework.models import CalibrationRecord
    with session_scope() as s:
        q = (
            select(Trade)
            .join(CalibrationRecord, CalibrationRecord.shadow_trade_id == Trade.id)
            .where(
                CalibrationRecord.paper_trade_id == paper_trade_id,
                Trade.fill_status == "open",
                Trade.mode == "shadow",
            )
        )
        t = s.execute(q).scalar_one_or_none()
        return t.id if t else None


def paper_id_for_shadow(shadow_trade_id: int) -> Optional[int]:
    """Inverse of find_open_shadow_for_paper — given a shadow trade id,
    return the paired paper trade id (regardless of paper's open/closed state).
    """
    from framework.models import CalibrationRecord
    with session_scope() as s:
        c = s.execute(
            select(CalibrationRecord).where(
                CalibrationRecord.shadow_trade_id == shadow_trade_id,
            )
        ).scalar_one_or_none()
        return c.paper_trade_id if c else None


def has_open_position(asset: str, venue: str, signal_type: str) -> bool:
    """True if there's an open paper Trade for this (asset, venue) opened from
    a signal of `signal_type`. Used by main loop to dedupe repeat signals.
    """
    with session_scope() as s:
        q = select(Trade, Signal).join(
            Signal, Signal.id == Trade.signal_id
        ).where(
            Trade.bot_id == BOT_ID,
            Trade.mode == "paper",
            Trade.fill_status == "open",
            Trade.asset == asset,
            Trade.venue == venue,
            Signal.signal_type == signal_type,
        )
        return s.execute(q).first() is not None
