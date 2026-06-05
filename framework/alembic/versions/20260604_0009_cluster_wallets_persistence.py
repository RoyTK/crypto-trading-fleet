"""cluster_wallets column on shadow_log + shadow_signals table

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-04

The 2026-06-02 ABGVN investigation revealed that the 4 mega-winner cluster
signals all had NULL wallet info because the bot was paused (no Signal row
written; cluster.py's wallet list was lost after the in-memory queue ticked).
This data gap blocks wallet-attribution analysis on any future mega-pump
while COPY remains paused.

Two-pronged fix:
1. Add cluster_wallets JSONB to copy_signal_shadow_log so the wallet list
   travels with every shadow-log entry going forward.
2. New shadow_signals table that mirrors the live signals table but is
   written REGARDLESS of cluster_buy_enabled. Captures the full signal
   payload (incl wallet_notionals) so we can reconstruct fire-time state
   for any past signal once enough data accumulates.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "copy_signal_shadow_log",
        sa.Column("cluster_wallets", sa.JSON(), nullable=True),
    )

    op.create_table(
        "shadow_signals",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("bot_id", sa.String(32), nullable=False),
        sa.Column("signal_type", sa.String(64), nullable=False),
        sa.Column("asset", sa.String(128), nullable=False),
        sa.Column("chain", sa.String(16), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("cluster_size", sa.Integer(), nullable=True),
        sa.Column("cluster_wallets", sa.JSON(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("cluster_uuid", sa.String(64), nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_unique_constraint(
        "uq_shadow_signals_cluster_uuid", "shadow_signals", ["cluster_uuid"]
    )
    op.create_index(
        "ix_shadow_signals_bot_fired",
        "shadow_signals",
        ["bot_id", "fired_at"],
    )
    op.create_index(
        "ix_shadow_signals_asset",
        "shadow_signals",
        ["asset", "fired_at"],
    )


def downgrade() -> None:
    op.drop_table("shadow_signals")
    op.drop_column("copy_signal_shadow_log", "cluster_wallets")
