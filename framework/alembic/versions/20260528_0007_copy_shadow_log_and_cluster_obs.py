"""copy_signal_shadow_log + cluster_observations tables

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-28

Two new tables (signed per adversarial team meeting 2026-05-28):

(1) copy_signal_shadow_log — per cluster signal, captures entry_price,
    multi-window prices (+30m/+1h/+4h/+12h), MFE, MAE, and metadata
    needed for the H1/H2 diagnostic (broken signal vs inverted signal).
    Populated by COPY's cluster fire-site + an APScheduler poller.

(2) cluster_observations — STRUCTURE's read-only journal of all
    cluster events published by COPY on the new `copy:all_clusters`
    channel. No gating logic on STRUCTURE — pure observation, no
    window reset.

Per engineer's R2 verdict: both tables are LOGGING only, no parameter
changes, no kill-criteria window reset implication.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "copy_signal_shadow_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("signal_id", sa.Integer(), nullable=True),  # signals.id when available
        sa.Column("cluster_uuid", sa.String(64), nullable=False),
        sa.Column("token_mint", sa.String(128), nullable=False),
        sa.Column("hl_asset_if_any", sa.String(16), nullable=True),
        sa.Column("cluster_size", sa.Integer(), nullable=False),
        sa.Column("cluster_total_notional_usd", sa.Float(), nullable=False),
        sa.Column("wallet_tier", sa.String(16), nullable=False),  # 'active' / 'mixed' / 'unknown'
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_price", sa.Numeric(28, 12), nullable=True),
        sa.Column("price_30m", sa.Numeric(28, 12), nullable=True),
        sa.Column("price_1h", sa.Numeric(28, 12), nullable=True),
        sa.Column("price_4h", sa.Numeric(28, 12), nullable=True),
        sa.Column("price_12h", sa.Numeric(28, 12), nullable=True),
        sa.Column("mfe_pct", sa.Float(), nullable=True),  # max favorable excursion within 12h
        sa.Column("mae_pct", sa.Float(), nullable=True),  # max adverse excursion within 12h
        sa.Column("mfe_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mae_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "status", sa.String(24), nullable=False, server_default="pending"
        ),  # pending | partial | complete | token_dead
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_unique_constraint(
        "uq_copy_shadow_cluster_uuid", "copy_signal_shadow_log", ["cluster_uuid"]
    )
    op.create_index(
        "ix_copy_shadow_fired_at", "copy_signal_shadow_log", ["fired_at"]
    )
    op.create_index(
        "ix_copy_shadow_pending", "copy_signal_shadow_log",
        ["status"], postgresql_where=sa.text("status IN ('pending', 'partial')"),
    )

    op.create_table(
        "cluster_observations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cluster_uuid", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("token_mint", sa.String(128), nullable=False),
        sa.Column("hl_asset_if_any", sa.String(16), nullable=True),
        sa.Column("cluster_size", sa.Integer(), nullable=False),
        sa.Column("cluster_total_notional_usd", sa.Float(), nullable=False),
        sa.Column("wallet_tier", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_unique_constraint(
        "uq_cluster_obs_uuid", "cluster_observations", ["cluster_uuid"]
    )
    op.create_index(
        "ix_cluster_obs_observed_at", "cluster_observations", ["observed_at"]
    )
    op.create_index(
        "ix_cluster_obs_hl_asset", "cluster_observations", ["hl_asset_if_any", "observed_at"]
    )


def downgrade() -> None:
    op.drop_table("cluster_observations")
    op.drop_table("copy_signal_shadow_log")
