"""cross_bot_signal_log table — COPY macro cluster → STRUCTURE HL perp outcome research

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-25

Passive research log. STRUCTURE subscribes to copy:macro_cluster events
(SOL/BTC/ETH only) and writes rows here. The outcome cron back-fills
price-at-4h/12h/24h + directional accuracy fields. No FK to signals
or trades — this is observation, not a live signal path.

If 7-day forward data shows 4h directional accuracy > 55% with N >= 15
evaluated events on at least one of SOL/BTC/ETH, the bridge thesis
graduates to a real STRUCTURE signal generator.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cross_bot_signal_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("cluster_id", sa.String(64), nullable=False),
        sa.Column("hl_asset", sa.String(16), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("wallet_count", sa.Integer(), nullable=False),
        sa.Column("cluster_size_usd", sa.Float(), nullable=False),
        sa.Column("event_timestamp_ms", sa.BigInteger(), nullable=False),
        sa.Column("entry_price_usd", sa.Float(), nullable=True),
        sa.Column("price_at_4h", sa.Float(), nullable=True),
        sa.Column("price_at_12h", sa.Float(), nullable=True),
        sa.Column("price_at_24h", sa.Float(), nullable=True),
        sa.Column("pnl_at_4h_pct", sa.Float(), nullable=True),
        sa.Column("pnl_at_12h_pct", sa.Float(), nullable=True),
        sa.Column("pnl_at_24h_pct", sa.Float(), nullable=True),
        sa.Column("direction_correct_4h", sa.Boolean(), nullable=True),
        sa.Column("direction_correct_12h", sa.Boolean(), nullable=True),
        sa.Column("direction_correct_24h", sa.Boolean(), nullable=True),
        sa.Column("outcome_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_cross_bot_signal_log_asset_ts",
        "cross_bot_signal_log",
        ["hl_asset", "event_timestamp_ms"],
    )
    op.create_index(
        "ix_cross_bot_signal_log_unevaluated",
        "cross_bot_signal_log",
        ["outcome_evaluated_at"],
    )
    op.create_unique_constraint(
        "uq_cross_bot_cluster_id",
        "cross_bot_signal_log",
        ["cluster_id"],
    )


def downgrade() -> None:
    op.drop_table("cross_bot_signal_log")
