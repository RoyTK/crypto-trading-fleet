"""cluster_detections table — persistent dedup primitive

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-30

Adds `cluster_detections` to enforce one fire per (chain, token, signal_type,
direction, window_bucket). Window_bucket size is configurable via env var
COPY_CLUSTER_DEDUP_HOURS (default 24h). Suppressed re-detects are also
persisted (fired=false, suppressed_reason='dedup_window') for audit + the
dashboard panel that compares N_unique under different dedup-hour assumptions.

Background: 2026-05-30 brainstorm verdict — the existing N=64 shadow log
collapses to N_unique=24 (37.5%). The in-memory _already_fired dict in
cluster.py (15-min suppression) correctly prevents within-window re-emits
but does nothing across the 16-day shadow log range. This table is the
data-hygiene primitive that lets us accumulate clean independent observations
before re-evaluating exit configs.

Per the kill-criteria reset rule: this is data-hygiene infrastructure (does
NOT reset the window).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cluster_detections",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cluster_uuid", sa.String(64), nullable=False),
        sa.Column("chain", sa.String(16), nullable=False),
        sa.Column("token_mint", sa.String(128), nullable=False),
        sa.Column("signal_type", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("cluster_size", sa.Integer(), nullable=False),
        sa.Column("cluster_total_notional_usd", sa.Float(), nullable=False),
        sa.Column("wallet_tier", sa.String(16), nullable=False),
        sa.Column("window_bucket", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dedup_hours", sa.Integer(), nullable=False),
        sa.Column("dedup_key", sa.String(128), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fired", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("suppressed_reason", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_unique_constraint(
        "uq_cluster_detections_uuid", "cluster_detections", ["cluster_uuid"]
    )
    # Partial UNIQUE on dedup_key for fired=true rows ONLY. Lets us write
    # multiple suppressed (fired=false) rows with the same dedup_key as the
    # original fire — preserves the audit trail of "we detected this same
    # cluster N times within the dedup window."
    op.create_index(
        "uq_cluster_detections_dedup_key_fired",
        "cluster_detections",
        ["dedup_key"],
        unique=True,
        postgresql_where=sa.text("fired = true"),
    )
    op.create_index(
        "ix_cluster_detections_token_time",
        "cluster_detections",
        ["token_mint", "detected_at"],
    )
    op.create_index(
        "ix_cluster_detections_window_bucket",
        "cluster_detections",
        ["window_bucket"],
    )
    op.create_index(
        "ix_cluster_detections_fired",
        "cluster_detections",
        ["fired", "detected_at"],
    )


def downgrade() -> None:
    op.drop_table("cluster_detections")
