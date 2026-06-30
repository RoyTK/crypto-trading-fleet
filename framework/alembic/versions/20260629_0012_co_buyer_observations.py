"""co_buyer_observations — cross-token co-buyer corpus (Solscan early-buyer observations)

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-29

Server-side home for the co-buyer corpus that browser-Opus appends to OneDrive as
JSONL. A local ingest (scripts/ingest_co_buyer_obs.py) loads those observations here
so the recurring-team correlation can JOIN our live data — wallet_swaps_log, trades,
wallet_attributions. One row per (followable token x pre-run early buyer). dedup_key
(token|wallet|first_buy) makes re-ingest idempotent.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "co_buyer_observations",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("dedup_key", sa.String(256), nullable=False),
        sa.Column("token", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=True),
        sa.Column("run_date", sa.Date, nullable=True),
        sa.Column("wallet", sa.String(64), nullable=False),
        sa.Column("first_buy", sa.Date, nullable=True),
        sa.Column("held_into_run", sa.Boolean, nullable=True),
        sa.Column("entry_liq_usd", sa.Float, nullable=True),
        sa.Column("run_liq_usd", sa.Float, nullable=True),
        sa.Column("source", sa.String(64), nullable=True),
        sa.Column("observed", sa.Date, nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_cobuyer_obs_dedup", "co_buyer_observations", ["dedup_key"], unique=True)
    op.create_index("ix_cobuyer_obs_token", "co_buyer_observations", ["token"])
    op.create_index("ix_cobuyer_obs_wallet", "co_buyer_observations", ["wallet"])


def downgrade() -> None:
    op.drop_index("ix_cobuyer_obs_wallet", table_name="co_buyer_observations")
    op.drop_index("ix_cobuyer_obs_token", table_name="co_buyer_observations")
    op.drop_index("ix_cobuyer_obs_dedup", table_name="co_buyer_observations")
    op.drop_table("co_buyer_observations")
