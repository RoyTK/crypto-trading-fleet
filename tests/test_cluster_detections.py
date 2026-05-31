"""Pure-logic tests for the cluster_detections dedup primitive.

Covers the pure helpers (compute_window_bucket, compute_dedup_key) without
touching the DB. The DB-side write_cluster_detection upsert is exercised
end-to-end by the bot loop + the diagnostic SQL Roy can run post-deploy.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://nope:nope@localhost/nope")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from bots.copy.loop_helpers import compute_dedup_key, compute_window_bucket


# ---------- compute_window_bucket -------------------------------------------

def test_24h_bucket_floors_to_midnight_utc():
    ts = datetime(2026, 5, 30, 14, 37, 12, 123, tzinfo=timezone.utc)
    bucket = compute_window_bucket(ts, dedup_hours=24)
    assert bucket == datetime(2026, 5, 30, 0, 0, 0, tzinfo=timezone.utc)


def test_4h_bucket_floors_to_4h_boundary():
    cases = [
        (datetime(2026, 5, 30, 0, 0, 0, tzinfo=timezone.utc),  0),
        (datetime(2026, 5, 30, 3, 59, 59, tzinfo=timezone.utc), 0),
        (datetime(2026, 5, 30, 4, 0, 0, tzinfo=timezone.utc),  4),
        (datetime(2026, 5, 30, 7, 30, 0, tzinfo=timezone.utc), 4),
        (datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc), 12),
        (datetime(2026, 5, 30, 23, 59, 59, tzinfo=timezone.utc), 20),
    ]
    for ts, expected_hour in cases:
        bucket = compute_window_bucket(ts, dedup_hours=4)
        assert bucket.hour == expected_hour, f"{ts} expected hour {expected_hour}, got {bucket.hour}"


def test_1h_bucket_floors_to_hour_mark():
    ts = datetime(2026, 5, 30, 14, 37, 12, tzinfo=timezone.utc)
    bucket = compute_window_bucket(ts, dedup_hours=1)
    assert bucket == datetime(2026, 5, 30, 14, 0, 0, tzinfo=timezone.utc)


def test_zero_dedup_hours_degenerates_to_per_second():
    """dedup_hours=0 should NOT bucket — every detection becomes its own row."""
    ts1 = datetime(2026, 5, 30, 14, 37, 12, 123, tzinfo=timezone.utc)
    ts2 = ts1 + timedelta(seconds=1)
    b1 = compute_window_bucket(ts1, dedup_hours=0)
    b2 = compute_window_bucket(ts2, dedup_hours=0)
    assert b1 != b2


def test_naive_datetime_is_treated_as_utc():
    """Defensive: SignalCandidate timestamps should be UTC but a naive ts
    arriving from old code paths should not crash the dedup primitive."""
    naive = datetime(2026, 5, 30, 14, 0, 0)  # no tzinfo
    bucket = compute_window_bucket(naive, dedup_hours=24)
    assert bucket.tzinfo is not None
    assert bucket.hour == 0


# ---------- compute_dedup_key -----------------------------------------------

def test_dedup_key_is_deterministic_for_same_inputs():
    bucket = datetime(2026, 5, 30, 0, 0, 0, tzinfo=timezone.utc)
    k1 = compute_dedup_key(
        chain="solana", token_mint="7m96tz...", signal_type="cluster_buy",
        direction="long", window_bucket=bucket,
    )
    k2 = compute_dedup_key(
        chain="solana", token_mint="7m96tz...", signal_type="cluster_buy",
        direction="long", window_bucket=bucket,
    )
    assert k1 == k2


def test_dedup_key_differs_by_token():
    bucket = datetime(2026, 5, 30, 0, 0, 0, tzinfo=timezone.utc)
    k1 = compute_dedup_key(
        chain="solana", token_mint="TOKEN_A", signal_type="cluster_buy",
        direction="long", window_bucket=bucket,
    )
    k2 = compute_dedup_key(
        chain="solana", token_mint="TOKEN_B", signal_type="cluster_buy",
        direction="long", window_bucket=bucket,
    )
    assert k1 != k2


def test_dedup_key_differs_by_direction():
    """Long and short signals on the same token in the same bucket are NOT
    the same observation — they should produce distinct dedup keys."""
    bucket = datetime(2026, 5, 30, 0, 0, 0, tzinfo=timezone.utc)
    k_long = compute_dedup_key(
        chain="solana", token_mint="X", signal_type="cluster_buy",
        direction="long", window_bucket=bucket,
    )
    k_short = compute_dedup_key(
        chain="solana", token_mint="X", signal_type="cluster_buy",
        direction="short", window_bucket=bucket,
    )
    assert k_long != k_short


def test_dedup_key_differs_by_signal_type():
    """Future sell-cluster detector will share the (chain, token, dir, bucket)
    tuple with buy clusters — signal_type must disambiguate."""
    bucket = datetime(2026, 5, 30, 0, 0, 0, tzinfo=timezone.utc)
    k_buy = compute_dedup_key(
        chain="solana", token_mint="X", signal_type="cluster_buy",
        direction="long", window_bucket=bucket,
    )
    k_sell = compute_dedup_key(
        chain="solana", token_mint="X", signal_type="cluster_sell",
        direction="long", window_bucket=bucket,
    )
    assert k_buy != k_sell


def test_dedup_key_differs_by_bucket():
    bucket_a = datetime(2026, 5, 30, 0, 0, 0, tzinfo=timezone.utc)
    bucket_b = datetime(2026, 5, 31, 0, 0, 0, tzinfo=timezone.utc)
    k_a = compute_dedup_key(
        chain="solana", token_mint="X", signal_type="cluster_buy",
        direction="long", window_bucket=bucket_a,
    )
    k_b = compute_dedup_key(
        chain="solana", token_mint="X", signal_type="cluster_buy",
        direction="long", window_bucket=bucket_b,
    )
    assert k_a != k_b


# ---------- Integration: the bucketing math collapses the real shadow_log ----

def test_bucketing_collapses_known_re_fires():
    """Replays the diagnostic finding: 24h dedup buckets each calendar
    day separately, so a token that fires twice on the same UTC day shares
    a bucket while same-token fires across days do not."""
    bucket = lambda ts: compute_window_bucket(ts, dedup_hours=24)
    # Two fires within the same UTC day → same bucket
    same_day_1 = datetime(2026, 5, 30, 1, 0, 0, tzinfo=timezone.utc)
    same_day_2 = datetime(2026, 5, 30, 23, 59, 0, tzinfo=timezone.utc)
    assert bucket(same_day_1) == bucket(same_day_2)
    # Cross-day fires → different buckets
    next_day = datetime(2026, 5, 31, 0, 1, 0, tzinfo=timezone.utc)
    assert bucket(same_day_2) != bucket(next_day)


def test_bucketing_under_4h_separates_intra_day_clusters():
    """Sensitivity test for the Grafana comparison panel: 4h dedup keeps
    morning/afternoon/evening cluster bursts as independent observations."""
    bucket = lambda ts: compute_window_bucket(ts, dedup_hours=4)
    morning = datetime(2026, 5, 30, 2, 0, 0, tzinfo=timezone.utc)
    afternoon = datetime(2026, 5, 30, 14, 0, 0, tzinfo=timezone.utc)
    assert bucket(morning) != bucket(afternoon)
