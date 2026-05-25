"""bot_state.kill_criteria_status JSON column

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-25

Adds a JSON column to bot_state for kill/promotion criteria machinery.
Updated hourly by framework.kill_criteria_monitor. Stores: current WR,
N, net PnL, slippage ratio, signal rate, list of fired triggers, list
of within-warning-margin triggers, promotion eligibility, last-checked
timestamp. Read by Grafana for the "kill criteria status" panel.

Per Roy's note 2026-05-25 ("I think this is overkill but no harm; I
will rely on my judgment in the end"): the machinery ALERTS only, it
does NOT auto-halt. The halt_bot() integration deliberately omitted.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "bot_state",
        sa.Column("kill_criteria_status", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bot_state", "kill_criteria_status")
