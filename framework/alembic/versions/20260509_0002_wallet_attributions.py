"""wallet_attributions table for per-wallet PnL scoring (COPY bot)

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wallet_attributions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wallet_address", sa.String(64), nullable=False),
        sa.Column("chain", sa.String(16), nullable=False),
        sa.Column("bot_id", sa.String(32), nullable=False, server_default="copy"),
        sa.Column(
            "trade_id",
            sa.Integer,
            sa.ForeignKey("trades.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("signal_id", sa.Integer, sa.ForeignKey("signals.id"), nullable=True),
        sa.Column("cluster_size", sa.Integer, nullable=False),
        sa.Column("attributed_pnl_usd", sa.Float, nullable=False),
        sa.Column("attributed_pnl_pct", sa.Float, nullable=False),
        sa.Column("notional_contribution_usd", sa.Float, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_wallet_attr_wallet", "wallet_attributions", ["wallet_address"])
    op.create_index("ix_wallet_attr_trade", "wallet_attributions", ["trade_id"])
    op.create_index("ix_wallet_attr_chain_bot", "wallet_attributions", ["chain", "bot_id"])


def downgrade() -> None:
    op.drop_index("ix_wallet_attr_chain_bot", table_name="wallet_attributions")
    op.drop_index("ix_wallet_attr_trade", table_name="wallet_attributions")
    op.drop_index("ix_wallet_attr_wallet", table_name="wallet_attributions")
    op.drop_table("wallet_attributions")
