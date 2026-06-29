"""wallet_swaps_log — raw per-swap (buy/sell) log with the token, for slow-cluster research

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-29

Adds wallet_swaps_log: a passive log of every matched tracked-wallet swap
(buys + sells) WITH the token_mint, written by the webhook receiver. Unlike
wallet_events_log (wallet + time only), this retains wallet + token + side +
notional so we can detect MULTI-DAY GROUP accumulation — the "slow cluster"
signal that cluster's ~15-min co-buy window cannot see (a group loading the
same token over several days before a pump). Shadow research data only; the
daily wallet-pool cron truncates it to a bounded window.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wallet_swaps_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("wallet_address", sa.String(64), nullable=False),
        sa.Column("chain", sa.String(16), nullable=False, server_default="solana"),
        sa.Column("token_mint", sa.String(128), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("notional_usd", sa.Float, nullable=False),
        sa.Column("source_webhook", sa.String(16), nullable=False),
        sa.Column("tx_signature", sa.String(128), nullable=True),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_wallet_swaps_token_time", "wallet_swaps_log",
                    ["token_mint", "event_at"])
    op.create_index("ix_wallet_swaps_wallet_time", "wallet_swaps_log",
                    ["wallet_address", "event_at"])
    op.create_index("ix_wallet_swaps_time", "wallet_swaps_log", ["event_at"])


def downgrade() -> None:
    op.drop_index("ix_wallet_swaps_time", table_name="wallet_swaps_log")
    op.drop_index("ix_wallet_swaps_wallet_time", table_name="wallet_swaps_log")
    op.drop_index("ix_wallet_swaps_token_time", table_name="wallet_swaps_log")
    op.drop_table("wallet_swaps_log")
