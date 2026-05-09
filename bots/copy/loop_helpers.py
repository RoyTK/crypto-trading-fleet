"""Persistence helpers for the COPY main loop.

Mirrors bots/structure/loop_helpers.py with the COPY signal candidate type.
Bots write to `signals` and `trades` only — never `scores`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from bots.base.fill_simulator_base import SimulatedFill
from bots.copy.signals.base import SignalCandidate
from framework.audit import write_audit
from framework.db import session_scope
from framework.logging_setup import get_logger
from framework.models import Signal, Trade, WalletAttribution


_log = get_logger(__name__)


BOT_ID = "copy"


@dataclass
class OpenPaperTrade:
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
    leverage: float = 1.0,
) -> Optional[int]:
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
                "cluster_size": candidate.cluster_size,
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
            "chain": candidate.chain,
            "direction": candidate.direction,
            "size_usd": notional_usd,
            "cluster_size": candidate.cluster_size,
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
    with session_scope() as s:
        t = s.get(Trade, trade_id)
        if t is None or t.fill_status != "open":
            return
        t.exit_price = exit_price
        t.exit_at = datetime.now(timezone.utc)
        t.exit_reason = exit_reason
        t.fill_status = "closed"
        t.fees_usd = float(t.fees_usd or 0.0) + exit_fill.fees_usd

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
    # Attribute the closed trade's PnL to the wallets that triggered the cluster.
    # Best-effort — failure must NOT break the close path.
    try:
        attribute_closed_trade(trade_id)
    except Exception:
        _log.exception("attribution_failed", trade_id=trade_id)


def attribute_closed_trade(trade_id: int) -> int:
    """Write per-wallet PnL attribution rows for a closed paper trade.

    Equal-share attribution: trade.pnl_usd is split evenly across all wallets
    that participated in the originating cluster signal. Wallet-specific
    notional contribution from the signal payload is recorded for future
    weighted-attribution variants.

    Idempotent: skips if attribution rows already exist for this trade_id.
    Returns the number of rows written.
    """
    with session_scope() as s:
        t = s.get(Trade, trade_id)
        if t is None or t.fill_status != "closed" or t.bot_id != BOT_ID:
            return 0
        if t.pnl_usd is None or t.pnl_pct is None:
            return 0
        if t.signal_id is None:
            return 0

        # Idempotency check
        existing = s.execute(
            select(WalletAttribution.id).where(WalletAttribution.trade_id == trade_id).limit(1)
        ).first()
        if existing is not None:
            return 0

        sig = s.get(Signal, t.signal_id)
        if sig is None or not sig.payload:
            return 0
        wallets = sig.payload.get("wallets") or []
        cluster_size = int(sig.payload.get("cluster_size") or len(wallets))
        if not wallets or cluster_size == 0:
            return 0

        # If the signal payload included per-wallet notional in `wallet_notionals`
        # (future extension), use it for the contribution field. Otherwise leave null.
        wallet_notionals = sig.payload.get("wallet_notionals") or {}

        per_wallet_pnl = float(t.pnl_usd) / cluster_size
        per_wallet_pct = float(t.pnl_pct)  # pct is per-trade, same for every wallet
        chain = sig.venue or t.venue or "solana"

        n = 0
        for wallet_addr in wallets:
            if not isinstance(wallet_addr, str):
                continue
            attr = WalletAttribution(
                wallet_address=wallet_addr,
                chain=chain,
                bot_id=BOT_ID,
                trade_id=trade_id,
                signal_id=t.signal_id,
                cluster_size=cluster_size,
                attributed_pnl_usd=per_wallet_pnl,
                attributed_pnl_pct=per_wallet_pct,
                notional_contribution_usd=wallet_notionals.get(wallet_addr),
            )
            s.add(attr)
            n += 1
        return n


def panic_close_all_open() -> int:
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
        write_audit("panic_paper_trades_closed", bot_id=BOT_ID, payload={"count": n})
    return n


def has_open_position(asset: str, venue: str) -> bool:
    """Dedupe: true if we already have an open paper trade for this token."""
    with session_scope() as s:
        q = select(Trade).where(
            Trade.bot_id == BOT_ID,
            Trade.mode == "paper",
            Trade.fill_status == "open",
            Trade.asset == asset,
            Trade.venue == venue,
        )
        return s.execute(q).first() is not None


def open_allocation_pct(paper_capital_usd: float) -> float:
    """Sum of size_usd of currently open paper trades, as % of paper capital."""
    if paper_capital_usd <= 0:
        return 0.0
    with session_scope() as s:
        from sqlalchemy import func
        total = s.execute(
            select(func.coalesce(func.sum(Trade.size_usd), 0.0)).where(
                Trade.bot_id == BOT_ID,
                Trade.mode == "paper",
                Trade.fill_status == "open",
            )
        ).scalar() or 0.0
    return float(total) / paper_capital_usd * 100.0
