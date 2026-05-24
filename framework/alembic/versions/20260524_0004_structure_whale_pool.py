"""structure_whale_pool table — replaces bots/structure/whale_list.json as source of truth

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-24

Why DB instead of JSON: cron-managed JSON files in a git-tracked path get
wiped whenever `git reset --hard origin/main` runs (the cron's appends are
never committed back). Lost 29 of 50 whales this way on 2026-05-24. DB
table is immune to git ops, matches the COPY wallet_pool architecture
shipped two days ago.

STRUCTURE's pool is simpler than COPY's — we poll position state every
60s via Hyperliquid Info API (no webhooks → no event counting). Just
need: address, tier (working/premium), curation metrics snapshot,
pinning, soft-delete.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "structure_whale_pool",
        sa.Column("address", sa.String(64), primary_key=True),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("source", sa.String(64), nullable=True),
        # Tier: 'working' = $2M/$250k (default), 'premium' = $5M/$500k
        sa.Column(
            "tier", sa.String(16), nullable=False, server_default="working"
        ),
        # Curation metrics — snapshot from curate_whales_v2 / quarterly refresh.
        sa.Column("tag", sa.String(128), nullable=True),
        sa.Column("historical_win_rate", sa.Float, nullable=True),
        sa.Column("closed_positions_6mo", sa.Integer, nullable=True),
        sa.Column("cumulative_notional_usd", sa.Float, nullable=True),
        sa.Column("avg_hold_minutes", sa.Float, nullable=True),
        sa.Column("current_max_position_usd", sa.Float, nullable=True),
        sa.Column("metrics_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        # Manual override
        sa.Column("pinned", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pinned_reason", sa.Text, nullable=True),
        # Soft-delete — bot only loads rows where pruned_at IS NULL
        sa.Column("pruned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pruned_reason", sa.Text, nullable=True),
    )
    # Index for bot's "load active whales" query (most common path)
    op.create_index(
        "ix_structure_whale_pool_active",
        "structure_whale_pool",
        ["tier"],
        postgresql_where=sa.text("pruned_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_structure_whale_pool_active", table_name="structure_whale_pool"
    )
    op.drop_table("structure_whale_pool")
