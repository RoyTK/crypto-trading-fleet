"""Compare N_unique under different dedup_hours assumptions.

Run anytime after the cluster_detections table has accumulated data:

  docker compose exec bot_copy python -m scripts.cluster_detections_diagnostic

  # restrict to last N days
  docker compose exec bot_copy python -m scripts.cluster_detections_diagnostic --since-days 30

Prints a table similar to:

  dedup_hours | n_fired | n_suppressed | n_unique_buckets | unique_pct
  -----------+---------+--------------+------------------+-----------
       1     |   200   |      14      |       175        |   87.5
       4     |   180   |      34      |       155        |   77.5
      12     |   140   |      74      |       120        |   60.0
      24     |   115   |      99      |        95        |   47.5

The "n_unique_buckets" column treats the dedup_hours value as a HYPOTHESIS
and counts unique (chain, token, signal_type, direction, bucket) tuples
in the SAME raw data. This lets us compare without re-running the bot
under different config values.

unique_pct = n_unique_buckets / (n_fired + n_suppressed). Statistician's
hard ship rule: do NOT ship exit redesign on a dataset where unique_pct
< 50%. The pre-build diagnostic on copy_signal_shadow_log returned 37.5%
on 2026-05-30. This script gives the post-deploy view on cluster_detections
where every detection (fired AND suppressed) is recorded.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", os.environ.get("DATABASE_URL", ""))
os.environ.setdefault("REDIS_URL", os.environ.get("REDIS_URL", "redis://localhost:6379/0"))

from sqlalchemy import text

from framework.db import session_scope


DEDUP_HYPOTHESES = (1, 4, 12, 24, 72)


def _query_rows(since: datetime) -> list[dict]:
    """Pull every cluster_detections row in the window."""
    sql = text("""
        SELECT chain, token_mint, signal_type, direction,
               detected_at, fired
        FROM cluster_detections
        WHERE detected_at >= :since
        ORDER BY detected_at ASC
    """)
    with session_scope() as s:
        rows = s.execute(sql, {"since": since}).all()
    return [
        {
            "chain": r.chain, "token": r.token_mint, "signal_type": r.signal_type,
            "direction": r.direction, "detected_at": r.detected_at, "fired": r.fired,
        }
        for r in rows
    ]


def _bucket(ts: datetime, dedup_hours: int) -> datetime:
    """Re-implementation of compute_window_bucket (decoupled to keep this
    script runnable when only framework/db is importable)."""
    if dedup_hours <= 0:
        return ts.replace(microsecond=0)
    ts_utc = ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    midnight = ts_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    hour_offset = (ts_utc.hour // dedup_hours) * dedup_hours
    return midnight.replace(hour=hour_offset)


def _summarize(rows: list[dict]) -> None:
    if not rows:
        print("(no rows — cluster_detections is empty in the window)", file=sys.stderr)
        return

    fired_total = sum(1 for r in rows if r["fired"])
    suppressed_total = sum(1 for r in rows if not r["fired"])
    total = fired_total + suppressed_total

    print(f"\n=== cluster_detections diagnostic — {total} detections "
          f"({fired_total} fired / {suppressed_total} suppressed) ===\n", file=sys.stderr)
    print(f"{'dedup_hours':>11}  {'n_unique':>9}  {'unique_pct':>10}  {'verdict':>30}",
          file=sys.stderr)
    print("-" * 70, file=sys.stderr)
    for h in DEDUP_HYPOTHESES:
        buckets = set()
        for r in rows:
            b = _bucket(r["detected_at"], h)
            buckets.add((r["chain"], r["token"], r["signal_type"], r["direction"], b))
        n_unique = len(buckets)
        pct = (100.0 * n_unique / total) if total > 0 else 0.0
        verdict = "OK for ship" if pct >= 50.0 else "BELOW kill threshold"
        marker = "✓" if pct >= 50.0 else "✗"
        print(f"{h:>11}  {n_unique:>9}  {pct:>9.1f}%  {marker} {verdict}",
              file=sys.stderr)
    print(file=sys.stderr)


def _top_repeaters(rows: list[dict], dedup_hours: int, limit: int = 20) -> None:
    """Tokens with the most re-detections under the chosen dedup_hours."""
    from collections import Counter
    bucket_counter: Counter[tuple] = Counter()
    for r in rows:
        b = _bucket(r["detected_at"], dedup_hours)
        bucket_counter[(r["chain"], r["token"], r["signal_type"], r["direction"], b)] += 1
    # Aggregate by token
    token_repeats: dict[str, int] = {}
    for (_chain, token, _sig, _dir, _b), n in bucket_counter.items():
        if n > 1:
            token_repeats[token] = token_repeats.get(token, 0) + (n - 1)
    if not token_repeats:
        print(f"(no re-detections under {dedup_hours}h dedup)", file=sys.stderr)
        return
    print(f"\nTop tokens by re-detection count (suppressed @ {dedup_hours}h dedup):", file=sys.stderr)
    for token, n in sorted(token_repeats.items(), key=lambda kv: -kv[1])[:limit]:
        print(f"  {n:>4}  {token}", file=sys.stderr)
    print(file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since-days", type=int, default=30)
    parser.add_argument("--show-repeaters-at", type=int, default=24,
                        help="dedup_hours value to use for the top-repeaters table")
    args = parser.parse_args()

    since = datetime.now(timezone.utc) - timedelta(days=args.since_days)
    rows = _query_rows(since)
    _summarize(rows)
    _top_repeaters(rows, args.show_repeaters_at)
    return 0


if __name__ == "__main__":
    sys.exit(main())
