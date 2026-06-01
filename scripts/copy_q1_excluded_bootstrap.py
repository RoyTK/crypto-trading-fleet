"""Bootstrap Sharpe with/without Q1 (deep pre-curve) signals.

After 2026-06-01 finding that Q1 entry prices ($1.5e-5 to $5.4e-5) had
ZERO 10x events while Q2-Q4 had ALL of them, re-estimate Sharpe excluding
Q1. Statistician's decision rule: if Q2-Q4 CI lower bound clears 0.4,
the entry_price filter is statistically justified. If not, the apparent
edge may be Q1-only artifact (unlikely but worth checking).

Uses 4h forward return: (price_4h - entry_price) / entry_price.

Outputs a markdown summary to stderr; exits 0 if Q2-Q4 lower CI bound
clears 0.4 (ship the filter), 1 otherwise (don't ship).
"""
from __future__ import annotations

import os
import random
import statistics
import sys
from typing import Sequence

os.environ.setdefault("DATABASE_URL", os.environ.get("DATABASE_URL", ""))
os.environ.setdefault("REDIS_URL", os.environ.get("REDIS_URL", "redis://localhost:6379/0"))

from sqlalchemy import text

from framework.db import session_scope


N_BOOTSTRAP = 10_000
CI_ALPHA = 0.10  # 90% CI


def sharpe(arr: Sequence[float]) -> float:
    if len(arr) < 2:
        return 0.0
    m = statistics.mean(arr)
    sd = statistics.stdev(arr)
    return 0.0 if sd == 0.0 else m / sd


def bootstrap_sharpe(arr: Sequence[float], n_boot: int = N_BOOTSTRAP,
                     seed: int = 42) -> list[float]:
    if not arr:
        return []
    random.seed(seed)
    arr_list = list(arr)
    n = len(arr_list)
    out: list[float] = []
    for _ in range(n_boot):
        sample = [random.choice(arr_list) for _ in range(n)]
        out.append(sharpe(sample))
    return out


def ci_from_bootstrap(sharpes: Sequence[float], alpha: float = CI_ALPHA):
    if not sharpes:
        return (0.0, 0.0)
    s = sorted(sharpes)
    lo = s[max(0, int(len(s) * alpha / 2))]
    hi = s[min(len(s) - 1, int(len(s) * (1 - alpha / 2)))]
    return (lo, hi)


def main() -> int:
    with session_scope() as s:
        rows = s.execute(text("""
            WITH quartiled AS (
              SELECT entry_price, mfe_pct, mae_pct, price_4h, price_12h,
                     (price_4h / entry_price) - 1.0 AS ret_4h,
                     (price_12h / entry_price) - 1.0 AS ret_12h,
                     NTILE(4) OVER (ORDER BY entry_price) AS q
              FROM copy_signal_shadow_log
              WHERE status='complete' AND entry_price > 0 AND price_4h > 0
            )
            SELECT q, ret_4h, ret_12h FROM quartiled
        """)).all()

    if not rows:
        print("ERROR: no completed shadow_log rows with price_4h", file=sys.stderr)
        return 2

    by_q: dict[int, dict[str, list[float]]] = {q: {"4h": [], "12h": []} for q in (1, 2, 3, 4)}
    all_4h: list[float] = []
    all_12h: list[float] = []
    for r in rows:
        q = int(r.q)
        r4 = float(r.ret_4h) if r.ret_4h is not None else None
        r12 = float(r.ret_12h) if r.ret_12h is not None else None
        if r4 is not None:
            by_q[q]["4h"].append(r4)
            all_4h.append(r4)
        if r12 is not None:
            by_q[q]["12h"].append(r12)
            all_12h.append(r12)

    q234_4h = by_q[2]["4h"] + by_q[3]["4h"] + by_q[4]["4h"]
    q234_12h = by_q[2]["12h"] + by_q[3]["12h"] + by_q[4]["12h"]

    print(f"\n=== Bootstrap Sharpe (per-signal 4h forward return) ===\n", file=sys.stderr)
    print(f"{'Sample':<14} {'N':>5} {'Mean ret':>12} {'Sharpe':>8} "
          f"{'90% CI':>20}", file=sys.stderr)
    print("-" * 70, file=sys.stderr)
    for label, arr in [
        ("ALL", all_4h),
        ("Q1 only", by_q[1]["4h"]),
        ("Q2 only", by_q[2]["4h"]),
        ("Q3 only", by_q[3]["4h"]),
        ("Q4 only", by_q[4]["4h"]),
        ("Q2-Q4 combined", q234_4h),
    ]:
        if not arr:
            print(f"{label:<14} {'EMPTY':>5}", file=sys.stderr)
            continue
        mean_ret = statistics.mean(arr)
        point = sharpe(arr)
        boot = bootstrap_sharpe(arr)
        lo, hi = ci_from_bootstrap(boot)
        print(f"{label:<14} {len(arr):>5} {mean_ret*100:>+10.2f}% "
              f"{point:>+8.3f} [{lo:>+6.3f}, {hi:>+6.3f}]", file=sys.stderr)

    print(f"\n=== Same for 12h forward returns ===\n", file=sys.stderr)
    print(f"{'Sample':<14} {'N':>5} {'Mean ret':>12} {'Sharpe':>8} "
          f"{'90% CI':>20}", file=sys.stderr)
    print("-" * 70, file=sys.stderr)
    for label, arr in [
        ("ALL", all_12h),
        ("Q1 only", by_q[1]["12h"]),
        ("Q2-Q4 combined", q234_12h),
    ]:
        if not arr:
            print(f"{label:<14} {'EMPTY':>5}", file=sys.stderr)
            continue
        mean_ret = statistics.mean(arr)
        point = sharpe(arr)
        boot = bootstrap_sharpe(arr)
        lo, hi = ci_from_bootstrap(boot)
        print(f"{label:<14} {len(arr):>5} {mean_ret*100:>+10.2f}% "
              f"{point:>+8.3f} [{lo:>+6.3f}, {hi:>+6.3f}]", file=sys.stderr)

    # Decision gate
    q234_boot = bootstrap_sharpe(q234_4h)
    q234_lo, q234_hi = ci_from_bootstrap(q234_boot)
    print(f"\n=== Decision gate ===", file=sys.stderr)
    print(f"Q2-Q4 4h Sharpe 90% CI: [{q234_lo:.3f}, {q234_hi:.3f}]", file=sys.stderr)
    if q234_lo >= 0.4:
        print(f"✓ Lower bound {q234_lo:.3f} >= 0.4 → "
              f"entry_price filter IS statistically justified", file=sys.stderr)
        return 0
    elif q234_lo > 0:
        print(f"⚠ Lower bound {q234_lo:.3f} positive but <0.4 → "
              f"some lift over Q1 but not decisive", file=sys.stderr)
        return 1
    else:
        print(f"✗ Lower bound {q234_lo:.3f} <= 0 → "
              f"NO statistical justification for filter "
              f"(the apparent Q2-Q4 edge may be sample artifact)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
