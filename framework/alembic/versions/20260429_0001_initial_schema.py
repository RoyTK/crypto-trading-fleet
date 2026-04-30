"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bot_state",
        sa.Column("bot_id", sa.String(32), primary_key=True),
        sa.Column("state", sa.String(32), nullable=False, server_default="initializing"),
        sa.Column("shakedown_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("shakedown_passed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paper_clock_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paper_clock_day", sa.Integer, nullable=True),
        sa.Column("promotion_score", sa.Float, nullable=True),
        sa.Column("last_score_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("allocation_pct", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("halted_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("halt_reason", sa.Text, nullable=True),
        sa.Column("metadata_json", sa.JSON, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "signals",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("bot_id", sa.String(32), nullable=False),
        sa.Column("signal_type", sa.String(64), nullable=False),
        sa.Column("asset", sa.String(64), nullable=False),
        sa.Column("venue", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_signals_bot_created", "signals", ["bot_id", "created_at"])

    op.create_table(
        "trades",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("bot_id", sa.String(32), nullable=False),
        sa.Column("signal_id", sa.Integer, sa.ForeignKey("signals.id"), nullable=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("asset", sa.String(64), nullable=False),
        sa.Column("venue", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("entry_price", sa.Float, nullable=True),
        sa.Column("exit_price", sa.Float, nullable=True),
        sa.Column("size_usd", sa.Float, nullable=True),
        sa.Column("leverage", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("entry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fees_usd", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("pnl_usd", sa.Float, nullable=True),
        sa.Column("pnl_pct", sa.Float, nullable=True),
        sa.Column("exit_reason", sa.String(64), nullable=True),
        sa.Column("fill_status", sa.String(32), nullable=False, server_default="open"),
        sa.Column("sim_metadata", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_trades_bot_mode_created", "trades", ["bot_id", "mode", "created_at"])
    op.create_index("ix_trades_signal", "trades", ["signal_id"])

    op.create_table(
        "calibration_records",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("bot_id", sa.String(32), nullable=False),
        sa.Column("signal_id", sa.Integer, sa.ForeignKey("signals.id"), nullable=False),
        sa.Column("paper_trade_id", sa.Integer, sa.ForeignKey("trades.id"), nullable=True),
        sa.Column("shadow_trade_id", sa.Integer, sa.ForeignKey("trades.id"), nullable=True),
        sa.Column("sim_entry_price", sa.Float, nullable=True),
        sa.Column("actual_entry_price", sa.Float, nullable=True),
        sa.Column("sim_exit_price", sa.Float, nullable=True),
        sa.Column("actual_exit_price", sa.Float, nullable=True),
        sa.Column("sim_pnl_pct", sa.Float, nullable=True),
        sa.Column("actual_pnl_pct", sa.Float, nullable=True),
        sa.Column("calibration_ratio", sa.Float, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_calib_bot_created", "calibration_records", ["bot_id", "created_at"])

    op.create_table(
        "halts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("bot_id", sa.String(32), nullable=True),
        sa.Column("halt_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(8), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("metadata_json", sa.JSON, nullable=True),
        sa.Column("halted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resumed_by", sa.String(32), nullable=True),
    )
    op.create_index("ix_halts_scope_bot", "halts", ["scope", "bot_id"])
    op.create_index("ix_halts_halted_at", "halts", ["halted_at"])

    op.create_table(
        "scores",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("bot_id", sa.String(32), nullable=False),
        sa.Column("paper_clock_day", sa.Integer, nullable=True),
        sa.Column("return_score", sa.Float, nullable=False),
        sa.Column("risk_score", sa.Float, nullable=False),
        sa.Column("confidence_score", sa.Float, nullable=False),
        sa.Column("regime_score", sa.Float, nullable=False),
        sa.Column("calibration_score", sa.Float, nullable=False),
        sa.Column("promotion_score", sa.Float, nullable=False),
        sa.Column("floor_pass", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("floor_failures", sa.JSON, nullable=True),
        sa.Column("metadata_json", sa.JSON, nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_scores_bot_computed", "scores", ["bot_id", "computed_at"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("bot_id", sa.String(32), nullable=True),
        sa.Column("actor", sa.String(64), nullable=False, server_default="system"),
        sa.Column("payload", sa.JSON, nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_audit_event_created", "audit_log", ["event_type", "created_at"])
    op.create_index("ix_audit_bot_created", "audit_log", ["bot_id", "created_at"])

    op.create_table(
        "heartbeats",
        sa.Column("process_name", sa.String(64), primary_key=True),
        sa.Column("last_ping_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("metadata_json", sa.JSON, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("heartbeats")
    op.drop_index("ix_audit_bot_created", table_name="audit_log")
    op.drop_index("ix_audit_event_created", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_scores_bot_computed", table_name="scores")
    op.drop_table("scores")
    op.drop_index("ix_halts_halted_at", table_name="halts")
    op.drop_index("ix_halts_scope_bot", table_name="halts")
    op.drop_table("halts")
    op.drop_index("ix_calib_bot_created", table_name="calibration_records")
    op.drop_table("calibration_records")
    op.drop_index("ix_trades_signal", table_name="trades")
    op.drop_index("ix_trades_bot_mode_created", table_name="trades")
    op.drop_table("trades")
    op.drop_index("ix_signals_bot_created", table_name="signals")
    op.drop_table("signals")
    op.drop_table("bot_state")
