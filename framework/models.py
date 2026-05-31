from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Integer, BigInteger, Float, Boolean, DateTime, JSON, Text,
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
    kill_criteria_status = Column(JSON, nullable=True)
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


class CrossBotSignalLog(Base):
    """COPY macro cluster events logged by STRUCTURE for directional outcome research.

    Written by bots/structure/signals/cross_bot_subscriber.py (passive subscriber).
    Outcome columns back-filled by framework/cross_bot_outcome_cron.py via APScheduler
    every 4h. No FK to signals or trades — this is research log, not live signal path.
    """
    __tablename__ = "cross_bot_signal_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cluster_id = Column(String(64), nullable=False, unique=True)
    hl_asset = Column(String(16), nullable=False)
    direction = Column(String(8), nullable=False)
    wallet_count = Column(Integer, nullable=False)
    cluster_size_usd = Column(Float, nullable=False)
    event_timestamp_ms = Column(BigInteger, nullable=False)
    entry_price_usd = Column(Float, nullable=True)
    price_at_4h = Column(Float, nullable=True)
    price_at_12h = Column(Float, nullable=True)
    price_at_24h = Column(Float, nullable=True)
    pnl_at_4h_pct = Column(Float, nullable=True)
    pnl_at_12h_pct = Column(Float, nullable=True)
    pnl_at_24h_pct = Column(Float, nullable=True)
    direction_correct_4h = Column(Boolean, nullable=True)
    direction_correct_12h = Column(Boolean, nullable=True)
    direction_correct_24h = Column(Boolean, nullable=True)
    outcome_evaluated_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_cross_bot_signal_log_asset_ts", "hl_asset", "event_timestamp_ms"),
        Index("ix_cross_bot_signal_log_unevaluated", "outcome_evaluated_at"),
    )


class CopySignalShadowLog(Base):
    """Shadow log of every COPY cluster signal — H1/H2 diagnostic.

    Populated at signal fire-site; price columns + MFE/MAE filled by
    APScheduler poller. status='complete' once the 12h window elapses.
    Signed 2026-05-28 per adversarial team meeting.
    """
    __tablename__ = "copy_signal_shadow_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    signal_id = Column(Integer, nullable=True)
    cluster_uuid = Column(String(64), nullable=False, unique=True)
    token_mint = Column(String(128), nullable=False)
    hl_asset_if_any = Column(String(16), nullable=True)
    cluster_size = Column(Integer, nullable=False)
    cluster_total_notional_usd = Column(Float, nullable=False)
    wallet_tier = Column(String(16), nullable=False)
    fired_at = Column(DateTime(timezone=True), nullable=False)
    entry_price = Column(Float, nullable=True)  # Postgres NUMERIC → float on read
    price_30m = Column(Float, nullable=True)
    price_1h = Column(Float, nullable=True)
    price_4h = Column(Float, nullable=True)
    price_12h = Column(Float, nullable=True)
    mfe_pct = Column(Float, nullable=True)
    mae_pct = Column(Float, nullable=True)
    mfe_at = Column(DateTime(timezone=True), nullable=True)
    mae_at = Column(DateTime(timezone=True), nullable=True)
    sample_count = Column(Integer, nullable=False, default=0)
    status = Column(String(24), nullable=False, default="pending")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        Index("ix_copy_shadow_fired_at", "fired_at"),
    )


class ClusterObservation(Base):
    """STRUCTURE's read-only journal of every COPY cluster event.

    Subscribed from Redis `copy:all_clusters` channel. NOT gated by
    STRUCTURE — pure observation for future research. Per engineer R2
    verdict: pure logging, does NOT reset STRUCTURE's kill window.
    """
    __tablename__ = "cluster_observations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    cluster_uuid = Column(String(64), nullable=False, unique=True)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    token_mint = Column(String(128), nullable=False)
    hl_asset_if_any = Column(String(16), nullable=True)
    cluster_size = Column(Integer, nullable=False)
    cluster_total_notional_usd = Column(Float, nullable=False)
    wallet_tier = Column(String(16), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_cluster_obs_observed_at", "observed_at"),
        Index("ix_cluster_obs_hl_asset", "hl_asset_if_any", "observed_at"),
    )


class ClusterDetection(Base):
    """Persistent dedup primitive for cluster signals.

    One row per (chain, token, signal_type, direction, window_bucket).
    Window bucket size = COPY_CLUSTER_DEDUP_HOURS (default 24h).
    Suppressed re-detects also persisted (fired=false) so a Grafana panel
    can compare N_unique under different dedup-hour assumptions on the
    same dataset. Shipped 2026-05-30 after diagnostic showed shadow log
    N=64 collapses to N_unique=24 (37.5%, below Statistician's 50% kill
    threshold for shipping exit redesign).
    """
    __tablename__ = "cluster_detections"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    cluster_uuid = Column(String(64), nullable=False, unique=True)
    chain = Column(String(16), nullable=False)
    token_mint = Column(String(128), nullable=False)
    signal_type = Column(String(32), nullable=False)
    direction = Column(String(8), nullable=False)
    cluster_size = Column(Integer, nullable=False)
    cluster_total_notional_usd = Column(Float, nullable=False)
    wallet_tier = Column(String(16), nullable=False)
    window_bucket = Column(DateTime(timezone=True), nullable=False)
    dedup_hours = Column(Integer, nullable=False)
    # dedup_key uniqueness is enforced via partial unique index
    # (fired=true only) — see migration 0008.
    dedup_key = Column(String(128), nullable=False)
    detected_at = Column(DateTime(timezone=True), nullable=False)
    fired = Column(Boolean, nullable=False, default=True)
    suppressed_reason = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_cluster_detections_token_time", "token_mint", "detected_at"),
        Index("ix_cluster_detections_window_bucket", "window_bucket"),
        Index("ix_cluster_detections_fired", "fired", "detected_at"),
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


class StructureWhalePool(Base):
    """STRUCTURE bot's whale pool — replaces bots/structure/whale_list.json.

    Simpler than COPY's wallet_pool because STRUCTURE polls position state
    every 60s via HL Info API (no webhooks → no event-count tracking needed).
    Tier just distinguishes working ($2M/$250k) from premium ($5M/$500k)
    quality bars. Soft-delete via pruned_at so re-discovery can clear the
    flag instead of duplicating.
    """
    __tablename__ = "structure_whale_pool"

    address = Column(String(64), primary_key=True)
    added_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    source = Column(String(64), nullable=True)
    tier = Column(String(16), nullable=False, default="working")  # 'working' | 'premium'
    tag = Column(String(128), nullable=True)
    historical_win_rate = Column(Float, nullable=True)
    closed_positions_6mo = Column(Integer, nullable=True)
    cumulative_notional_usd = Column(Float, nullable=True)
    avg_hold_minutes = Column(Float, nullable=True)
    current_max_position_usd = Column(Float, nullable=True)
    metrics_refreshed_at = Column(DateTime(timezone=True), nullable=True)
    pinned = Column(Boolean, nullable=False, default=False)
    pinned_at = Column(DateTime(timezone=True), nullable=True)
    pinned_reason = Column(Text, nullable=True)
    pruned_at = Column(DateTime(timezone=True), nullable=True)
    pruned_reason = Column(Text, nullable=True)


class WalletPool(Base):
    """COPY bot's wallet pool with active/watch/pruned tier state.

    Active wallets are subscribed to the primary Helius webhook and their
    swap events feed the cluster detector. Watch wallets are subscribed to
    a secondary webhook and their events update event-count metadata only —
    they do NOT feed the cluster detector. Pruned wallets are unsubscribed
    entirely but the row is kept so re-discovery can reactivate them.

    Daily cron promotes/demotes/drops based on event counts + attribution
    history. See scripts/wallet_pool_daily_cron.py.
    """
    __tablename__ = "wallet_pool"

    address = Column(String(64), primary_key=True)
    chain = Column(String(16), nullable=False)
    tier = Column(String(16), nullable=False, default="watch")  # 'active' | 'watch' | 'pruned'
    added_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    last_event_at = Column(DateTime(timezone=True), nullable=True)
    events_30d = Column(Integer, nullable=False, default=0)
    events_total = Column(BigInteger, nullable=False, default=0)
    source = Column(String(64), nullable=True)
    promoted_at = Column(DateTime(timezone=True), nullable=True)
    demoted_at = Column(DateTime(timezone=True), nullable=True)
    cielo_pnl_90d_usd = Column(Float, nullable=True)
    cielo_winrate_90d = Column(Float, nullable=True)
    cielo_refreshed_at = Column(DateTime(timezone=True), nullable=True)
    last_attribution_at = Column(DateTime(timezone=True), nullable=True)
    pinned = Column(Boolean, nullable=False, default=False)
    pinned_at = Column(DateTime(timezone=True), nullable=True)
    pinned_reason = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_wallet_pool_tier", "tier"),
        Index("ix_wallet_pool_last_event", "last_event_at"),
    )


class WalletEventLog(Base):
    """Lightweight log of every webhook event seen for any pool wallet.

    Used to compute rolling 30d event counts in the daily cron. Older rows
    are truncated to keep the table bounded.
    """
    __tablename__ = "wallet_events_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    wallet_address = Column(String(64), nullable=False)
    event_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    source_webhook = Column(String(16), nullable=False)  # 'active' | 'watch'

    __table_args__ = (
        Index("ix_wallet_events_wallet_time", "wallet_address", "event_at"),
        Index("ix_wallet_events_time", "event_at"),
    )


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
