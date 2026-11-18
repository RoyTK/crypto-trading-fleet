"""wallet_vet_log — every API-vetter verdict, for calibration against our own P&L

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-17

scripts/discovery_weekly.py logs each vetted wallet's cluster + conviction verdict and the
metrics behind it. Roy's rule (2026-10-03): our P&L outranks any vet — this table lets us check
later whether the verdicts separated the wallets that made us money from the ones that didn't.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wallet_vet_log",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("address", sa.String(64), nullable=False),
        sa.Column("vetted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("cluster_verdict", sa.String(32), nullable=False),
        sa.Column("conviction_verdict", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("metrics", postgresql.JSONB, nullable=True),
    )
    op.create_index("ix_wallet_vet_log_address", "wallet_vet_log", ["address"])


def downgrade() -> None:
    op.drop_index("ix_wallet_vet_log_address", table_name="wallet_vet_log")
    op.drop_table("wallet_vet_log")
