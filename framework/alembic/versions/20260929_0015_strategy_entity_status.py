"""strategy_entity_status — per-strategy watch/promote lifecycle for wallets and teams

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-29

Generalises teamfollow's team watch lifecycle (Roy 2026-09-29) to every wallet-based
strategy: a wallet (conviction, swing) or team/cohort (cohortfire) that is a chronic
loser in ITS OWN strategy is moved to 'watch' — it keeps paper-trading on an isolated
'<strategy>_watch' track and is promoted back after re-proving on forward trades.
Absent row = active. `updated_at` is the demotion boundary for the forward re-prove.
(Teamfollow keeps its own teamfollow_team_status table; cluster uses wallet_pool.tier.)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "strategy_entity_status",
        sa.Column("strategy", sa.String(32), primary_key=True),
        sa.Column("entity", sa.String(64), primary_key=True),   # wallet address or team id
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("strategy_entity_status")
