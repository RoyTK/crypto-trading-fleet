"""wallet_pool + wallet_events_log tables for COPY active/watch tier architecture

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wallet_pool",
        sa.Column("address", sa.String(64), primary_key=True),
        sa.Column("chain", sa.String(16), nullable=False),
        sa.Column(
            "tier", sa.String(16), nullable=False, server_default="watch"
        ),  # 'active' | 'watch' | 'pruned'
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("events_30d", sa.Integer, nullable=False, server_default="0"),
        sa.Column("events_total", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("source", sa.String(64), nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("demoted_at", sa.DateTime(timezone=True), nullable=True),
        # Cielo validation snapshot (refreshed periodically)
        sa.Column("cielo_pnl_90d_usd", sa.Float, nullable=True),
        sa.Column("cielo_winrate_90d", sa.Float, nullable=True),
        sa.Column("cielo_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        # Attribution-proven protection: last time this wallet contributed to a
        # closed paper cluster trade with positive attributed PnL. Demotion
        # protection expires 1 year after this timestamp.
        sa.Column("last_attribution_at", sa.DateTime(timezone=True), nullable=True),
        # Manual override: when TRUE, daily cron will not demote / drop this
        # wallet regardless of metrics.
        sa.Column("pinned", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pinned_reason", sa.Text, nullable=True),
    )
    op.create_index("ix_wallet_pool_tier", "wallet_pool", ["tier"])
    op.create_index(
        "ix_wallet_pool_last_event", "wallet_pool", ["last_event_at"]
    )

    op.create_table(
        "wallet_events_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("wallet_address", sa.String(64), nullable=False),
        sa.Column(
            "event_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "source_webhook", sa.String(16), nullable=False
        ),  # 'active' | 'watch'
    )
    op.create_index(
        "ix_wallet_events_wallet_time",
        "wallet_events_log",
        ["wallet_address", sa.text("event_at DESC")],
    )
    op.create_index(
        "ix_wallet_events_time", "wallet_events_log", ["event_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_wallet_events_time", table_name="wallet_events_log")
    op.drop_index("ix_wallet_events_wallet_time", table_name="wallet_events_log")
    op.drop_table("wallet_events_log")
    op.drop_index("ix_wallet_pool_last_event", table_name="wallet_pool")
    op.drop_index("ix_wallet_pool_tier", table_name="wallet_pool")
    op.drop_table("wallet_pool")
