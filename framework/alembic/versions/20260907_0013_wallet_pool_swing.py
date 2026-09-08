"""wallet_pool.swing columns for the multi-day "swing-copy" COPY strategy

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-07

Adds a swing roster flag to wallet_pool. Swing-copy is a parallel COPY strategy
(conviction sibling) that follows multi-day-hold wallets IN and mirrors their
net-distribution exit OUT — validated by the 2026-08 follow-in/follow-out
backtest (follow beats mechanical, time-stable OOS). The roster is
`wallet_pool WHERE swing = true`; managed via scripts/set_swing_wallets.py.
Independent of `conviction` and `pinned` — a wallet may be any combination.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "wallet_pool",
        sa.Column("swing", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "wallet_pool",
        sa.Column("swing_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "wallet_pool",
        sa.Column("swing_reason", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_wallet_pool_swing",
        "wallet_pool",
        ["swing"],
        postgresql_where=sa.text("swing"),
    )


def downgrade() -> None:
    op.drop_index("ix_wallet_pool_swing", table_name="wallet_pool")
    op.drop_column("wallet_pool", "swing_reason")
    op.drop_column("wallet_pool", "swing_at")
    op.drop_column("wallet_pool", "swing")
