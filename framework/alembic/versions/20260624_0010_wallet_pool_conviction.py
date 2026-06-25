"""wallet_pool.conviction columns for the single-wallet "conviction" COPY mode

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-24

Adds a conviction roster flag to wallet_pool. The conviction strategy is a
parallel COPY mode that triggers on a SINGLE elite wallet's buy (no cluster)
with its own paper bankroll + isolated metrics. The roster is simply
`wallet_pool WHERE conviction = true`; managed via scripts/set_conviction_wallets.py.
Independent of `pinned` (which only governs demotion immunity).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "wallet_pool",
        sa.Column(
            "conviction", sa.Boolean, nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "wallet_pool",
        sa.Column("conviction_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "wallet_pool",
        sa.Column("conviction_reason", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_wallet_pool_conviction",
        "wallet_pool",
        ["conviction"],
        postgresql_where=sa.text("conviction"),
    )


def downgrade() -> None:
    op.drop_index("ix_wallet_pool_conviction", table_name="wallet_pool")
    op.drop_column("wallet_pool", "conviction_reason")
    op.drop_column("wallet_pool", "conviction_at")
    op.drop_column("wallet_pool", "conviction")
