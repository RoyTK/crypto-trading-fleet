from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, JSON, Text,
    Index, ForeignKey,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def _utcnow():
    return datetime.now(timezone.utc)


class BotState(Base):
    """One row per bot. Tracks lifecycle (paper, live, halted, killed) and clock."""
    __tablename__ = "bot_state"

    bot_id = Column(String(32), primary_key=True)  # 'structure', 'copy', 'event', 'sniper'
    state = Column(String(32), nullable=False, default="initializing")
    # initializing | shakedown | paper | promoted_l1 | promoted_l2 | promoted_l3 | halted | killed | extended_paper
    shakedown_started_at = Column(DateTime(timezone=True), nullable=True)
    shakedown_passed_at = Column(DateTime(timezone=True), nullable=True)
    paper_clock_started_at = Column(DateTime(timezone=True), nullable=True)
    paper_clock_day = Column(Integer, nullable=True)
    promotion_score = Column(Float, nullable=True)
    last_score_at = Column(DateTime(timezone=True), nullable=True)
    allocation_pct = Column(Float, nullable=False, default=0.0)
    halted_until = Column(DateTime(timezone=True), nullable=True)
    halt_reason = Column(Text, nullable=True)
    metadata_json = Column(JSON, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class Signal(Base):
    """Bot wrote a signal. May or may not result in a trade."""
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bot_id = Column(String(32), nullable=False)
    signal_type = Column(String(64), nullable=False)  # e.g. 'funding_fade', 'cluster_buy'
    asset = Column(String(64), nullable=False)
    venue = Column(String(32), nullable=False)
    direction = Column(String(8), nullable=False)  # 'long' | 'short'
    payload = Column(JSON, nullable=False)  # bot-specific signal context
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_signals_bot_created", "bot_id", "created_at"),
    )


class Trade(Base):
    """A simulated or real trade. mode='paper' / 'shadow' / 'live'."""
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bot_id = Column(String(32), nullable=False)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=True)
    mode = Column(String(16), nullable=False)  # 'paper' | 'shadow' | 'live'
    asset = Column(String(64), nullable=False)
    venue = Column(String(32), nullable=False)
    direction = Column(String(8), nullable=False)
    entry_price = Column(Float, nullable=True)
    exit_price = Column(Float, nullable=True)
    size_usd = Column(Float, nullable=True)
    leverage = Column(Float, nullable=False, default=1.0)
    entry_at = Column(DateTime(timezone=True), nullable=True)
    exit_at = Column(DateTime(timezone=True), nullable=True)
    fees_usd = Column(Float, nullable=False, default=0.0)
    pnl_usd = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)
    exit_reason = Column(String(64), nullable=True)  # 'stop' | 'tp' | 'manual' | 'panic' | 'timeout'
    fill_status = Column(String(32), nullable=False, default="open")  # 'open' | 'closed' | 'no_fill' | 'rejected'
    sim_metadata = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        Index("ix_trades_bot_mode_created", "bot_id", "mode", "created_at"),
        Index("ix_trades_signal", "signal_id"),
    )


class CalibrationRecord(Base):
    """Pairs simulated fill vs actual shadow fill for calibration ratio."""
    __tablename__ = "calibration_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bot_id = Column(String(32), nullable=False)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=False)
    paper_trade_id = Column(Integer, ForeignKey("trades.id"), nullable=True)
    shadow_trade_id = Column(Integer, ForeignKey("trades.id"), nullable=True)
    sim_entry_price = Column(Float, nullable=True)
    actual_entry_price = Column(Float, nullable=True)
    sim_exit_price = Column(Float, nullable=True)
    actual_exit_price = Column(Float, nullable=True)
    sim_pnl_pct = Column(Float, nullable=True)
    actual_pnl_pct = Column(Float, nullable=True)
    calibration_ratio = Column(Float, nullable=True)  # actual / sim
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_calib_bot_created", "bot_id", "created_at"),
    )


class Halt(Base):
    """Every halt event — per-bot DD, fleet DD, /panic, drift, manual."""
    __tablename__ = "halts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope = Column(String(16), nullable=False)  # 'bot' | 'fleet'
    bot_id = Column(String(32), nullable=True)  # null for fleet-wide
    halt_type = Column(String(64), nullable=False)
    # 'dd_daily' | 'dd_weekly' | 'dd_total' | 'consecutive_loss' | 'panic'
    # | 'drift' | 'heartbeat' | 'rpc_health' | 'manual'
    severity = Column(String(8), nullable=False)  # 'p0' | 'p1' | 'p2' | 'p3'
    reason = Column(Text, nullable=False)
    metadata_json = Column(JSON, nullable=True)
    halted_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    resumed_at = Column(DateTime(timezone=True), nullable=True)
    resumed_by = Column(String(32), nullable=True)  # 'auto' | 'manual:roy'

    __table_args__ = (
        Index("ix_halts_scope_bot", "scope", "bot_id"),
        Index("ix_halts_halted_at", "halted_at"),
    )


class Score(Base):
    """Snapshot of PromotionScore — written by scoring engine on a cadence."""
    __tablename__ = "scores"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bot_id = Column(String(32), nullable=False)
    paper_clock_day = Column(Integer, nullable=True)
    return_score = Column(Float, nullable=False)
    risk_score = Column(Float, nullable=False)
    confidence_score = Column(Float, nullable=False)
    regime_score = Column(Float, nullable=False)
    calibration_score = Column(Float, nullable=False)
    promotion_score = Column(Float, nullable=False)
    floor_pass = Column(Boolean, nullable=False, default=False)
    floor_failures = Column(JSON, nullable=True)  # list of failed floors
    metadata_json = Column(JSON, nullable=True)
    computed_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_scores_bot_computed", "bot_id", "computed_at"),
    )


class AuditLog(Base):
    """Append-only event log: heartbeat, override, panic, restart, anything notable."""
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(64), nullable=False)
    bot_id = Column(String(32), nullable=True)
    actor = Column(String(64), nullable=False, default="system")  # 'system' | 'roy' | 'agent:<name>'
    payload = Column(JSON, nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_audit_event_created", "event_type", "created_at"),
        Index("ix_audit_bot_created", "bot_id", "created_at"),
    )


class Heartbeat(Base):
    """Last-seen ping per process. Liveness watchdog reads this."""
    __tablename__ = "heartbeats"

    process_name = Column(String(64), primary_key=True)
    last_ping_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    metadata_json = Column(JSON, nullable=True)


class WalletAttribution(Base):
    """Per-wallet PnL attribution for cluster-buy paper trades (COPY bot).

    Each closed paper trade that originated from a multi-wallet cluster gets
    one row per participating wallet, with the trade's PnL attributed
    equal-share across the cluster. Aggregates of these rows feed the wallet
    leaderboard used to prune underperforming wallets from the COPY pool.
    """
    __tablename__ = "wallet_attributions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wallet_address = Column(String(64), nullable=False)
    chain = Column(String(16), nullable=False)
    bot_id = Column(String(32), nullable=False, default="copy")
    trade_id = Column(Integer, ForeignKey("trades.id", ondelete="CASCADE"), nullable=False)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=True)
    cluster_size = Column(Integer, nullable=False)
    attributed_pnl_usd = Column(Float, nullable=False)
    attributed_pnl_pct = Column(Float, nullable=False)
    notional_contribution_usd = Column(Float, nullable=True)  # this wallet's buy size in the cluster
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_wallet_attr_wallet", "wallet_address"),
        Index("ix_wallet_attr_trade", "trade_id"),
        Index("ix_wallet_attr_chain_bot", "chain", "bot_id"),
    )
