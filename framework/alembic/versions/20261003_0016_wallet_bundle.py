"""wallet_bundle — bot-farm detector output (wallets that buy together = one operator)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-03

scripts/bot_farm_detect.py (daily) finds tracked wallets whose buys land within 2 seconds of
another tracked wallet's buy of the same token >= 90% of the time, and groups them. One row per
flagged wallet; the table is rebuilt on every run. REPORT ONLY (Roy 2026-10-03): nothing trades
or culls on it — a profitable group is still one we keep.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wallet_bundle",
        sa.Column("address", sa.String(64), primary_key=True),
        sa.Column("bundle_id", sa.Integer, nullable=False),
        sa.Column("bundle_size", sa.Integer, nullable=False),
        sa.Column("buys", sa.Integer, nullable=False),            # buys in the lookback window
        sa.Column("joint_pct", sa.Float, nullable=False),         # % of its buys matched by a partner
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_wallet_bundle_bundle", "wallet_bundle", ["bundle_id"])


def downgrade() -> None:
    op.drop_index("ix_wallet_bundle_bundle", table_name="wallet_bundle")
    op.drop_table("wallet_bundle")
