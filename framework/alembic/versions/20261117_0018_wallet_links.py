"""wallet_links — wallets known to be one operator for reasons other than buy timing

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-17

The bot-farm detector groups wallets by simultaneous buys. Some operators split roles instead:
6z3k7Pz4 buys and forwards tokens to wQETjfST, which sells (found 2026-11-17 from transfers).
Links stored here are merged into the detector's groups on every run (wallet_bundle.link_reason).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wallet_links",
        sa.Column("a", sa.String(64), primary_key=True),
        sa.Column("b", sa.String(64), primary_key=True),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.add_column("wallet_bundle", sa.Column("link_reason", sa.Text, nullable=True))
    op.execute("""INSERT INTO wallet_links (a, b, reason) VALUES
        ('6z3k7Pz4syMkWSdB8PuReqzkCQ16vFipNTUVFCqDxVFp', 'wQETjfSTZrpLnSseZPJ7QHCbHjyRSg9z57ozw2Gkmvm',
         'transfers: 6z3k buys first and forwards tokens; wQETjfST sells 2.5-20 min later (2026-11-17)')""")


def downgrade() -> None:
    op.drop_column("wallet_bundle", "link_reason")
    op.drop_table("wallet_links")
