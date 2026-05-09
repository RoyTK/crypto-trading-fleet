"""COPY wallet leaderboard — rank wallets by attributed paper PnL.

Aggregates wallet_attributions rows per wallet and prints a ranked table.
The bottom-of-leaderboard wallets are candidates for pruning from the COPY
wallet pool.

Usage:
    docker compose exec -T framework python -m scripts.wallet_leaderboard
    docker compose exec -T framework python -m scripts.wallet_leaderboard --min-trades 5
    docker compose exec -T framework python -m scripts.wallet_leaderboard --bottom 20

Sample output columns:
    wallet_address  n  pnl_usd  win_rate  avg_pct  best  worst
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from sqlalchemy import text

from framework.db import session_scope


SQL = """
SELECT
    wallet_address,
    chain,
    COUNT(*) AS n_trades,
    ROUND(SUM(attributed_pnl_usd)::numeric, 2) AS total_pnl_usd,
    ROUND(AVG(attributed_pnl_pct)::numeric, 2) AS avg_pct,
    ROUND(MAX(attributed_pnl_pct)::numeric, 2) AS best_pct,
    ROUND(MIN(attributed_pnl_pct)::numeric, 2) AS worst_pct,
    COUNT(*) FILTER (WHERE attributed_pnl_usd > 0) AS wins,
    COUNT(*) FILTER (WHERE attributed_pnl_usd <= 0) AS losses
FROM wallet_attributions
WHERE bot_id = :bot_id
GROUP BY wallet_address, chain
HAVING COUNT(*) >= :min_trades
ORDER BY total_pnl_usd DESC
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", default="copy")
    parser.add_argument("--min-trades", type=int, default=1,
                        help="Show only wallets with at least N attributed trades (default 1)")
    parser.add_argument("--top", type=int, default=None,
                        help="Show only the top N (highest PnL) wallets")
    parser.add_argument("--bottom", type=int, default=None,
                        help="Show only the bottom N (lowest PnL) wallets")
    parser.add_argument("--csv", action="store_true",
                        help="Emit CSV instead of pretty-printed table")
    args = parser.parse_args()

    with session_scope() as s:
        rows = s.execute(text(SQL), {"bot_id": args.bot, "min_trades": args.min_trades}).all()

    if not rows:
        print("(no wallet_attributions rows yet — run backfill first or wait for closures)",
              file=sys.stderr)
        return 0

    selected = list(rows)
    if args.top is not None:
        selected = selected[: args.top]
    elif args.bottom is not None:
        selected = list(reversed(rows))[: args.bottom]

    if args.csv:
        print("wallet_address,chain,n_trades,total_pnl_usd,avg_pct,best_pct,worst_pct,wins,losses")
        for r in selected:
            wr = (r.wins / r.n_trades * 100.0) if r.n_trades else 0
            print(f"{r.wallet_address},{r.chain},{r.n_trades},{r.total_pnl_usd},"
                  f"{r.avg_pct},{r.best_pct},{r.worst_pct},{r.wins},{r.losses}")
        return 0

    header = f"{'wallet':<46} {'chain':<8} {'n':>4} {'pnl_usd':>10} {'avg%':>7} {'best%':>7} {'worst%':>7} {'WR':>6}"
    print(header)
    print("-" * len(header))
    for r in selected:
        wr = (r.wins / r.n_trades * 100.0) if r.n_trades else 0
        print(f"{r.wallet_address:<46} {r.chain:<8} {r.n_trades:>4} {float(r.total_pnl_usd):>10.2f} "
              f"{float(r.avg_pct):>7.2f} {float(r.best_pct):>7.2f} {float(r.worst_pct):>7.2f} {wr:>5.1f}%")

    # Summary line
    total_pnl = sum(float(r.total_pnl_usd) for r in selected)
    total_trades = sum(r.n_trades for r in selected)
    total_wins = sum(r.wins for r in selected)
    overall_wr = (total_wins / total_trades * 100.0) if total_trades else 0
    print("-" * len(header))
    print(f"{'TOTAL':<46} {'':<8} {total_trades:>4} {total_pnl:>10.2f} "
          f"{'':>7} {'':>7} {'':>7} {overall_wr:>5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
