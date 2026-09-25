"""strategy_changes (change log) + wallet_style (style classifier output)

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-25

strategy_changes: one row per deploy / config / roster change that affects a strategy.
Powers dashboard annotations (vertical markers) and the "before vs after each change"
tables, so every change can be judged on the trades that followed it.
  strategy = 'cluster' | 'conviction' | 'swing' | 'teamfollow' | 'cohortfire' | 'promobuy' | 'fleet'
  kind     = 'era_start' | 'fix' | 'config' | 'roster' | 'halt' | 'other'

wallet_style: latest style class per wallet from scripts/wallet_style_classify.py
(SNIPER / MM_HFT / MM_ESTABLISHED / SELECTOR / OTHER), so dashboards can show PnL by
wallet style and the pool composition.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "strategy_changes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("strategy", sa.String(32), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="other"),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("ref", sa.String(64), nullable=True),  # commit sha / halt id
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_strategy_changes_strat_time", "strategy_changes",
                    ["strategy", "changed_at"])
    op.create_index("ux_strategy_changes_dedupe", "strategy_changes",
                    ["strategy", "changed_at", "description"], unique=True)

    op.create_table(
        "wallet_style",
        sa.Column("address", sa.String(64), primary_key=True),
        sa.Column("cls", sa.String(16), nullable=False),
        sa.Column("median_age_h", sa.Float, nullable=True),
        sa.Column("n_aged", sa.Integer, nullable=True),
        sa.Column("swaps", sa.Integer, nullable=True),
        sa.Column("tokens", sa.Integer, nullable=True),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_wallet_style_cls", "wallet_style", ["cls"])


def downgrade() -> None:
    op.drop_index("ix_wallet_style_cls", table_name="wallet_style")
    op.drop_table("wallet_style")
    op.drop_index("ux_strategy_changes_dedupe", table_name="strategy_changes")
    op.drop_index("ix_strategy_changes_strat_time", table_name="strategy_changes")
    op.drop_table("strategy_changes")
