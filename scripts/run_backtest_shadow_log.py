"""CLI: replay shadow_log against different exit configs.

Lets you test 'what if exit was X' against the actual cluster signals
COPY's shadow log has captured. Doesn't fetch new price data — uses
the snapshots already in the shadow_log table.

Examples:

  # Current bot config (baseline)
  docker compose exec bot_copy python -m scripts.run_backtest_shadow_log \\
    --stop 8 --tp 30 --hold 12

  # No TP, full hold (let winners run)
  docker compose exec bot_copy python -m scripts.run_backtest_shadow_log \\
    --stop 8 --tp 999 --hold 12

  # Wider TP, tighter stop
  docker compose exec bot_copy python -m scripts.run_backtest_shadow_log \\
    --stop 8 --tp 200 --hold 12

  # No stop, no TP (pure 12h hold)
  docker compose exec bot_copy python -m scripts.run_backtest_shadow_log \\
    --no-stop --no-tp --hold 12

  # Sweep — try several configs and print a comparison table
  docker compose exec bot_copy python -m scripts.run_backtest_shadow_log --sweep

Default notional is $400 (matches COPY's typical position sizing on $10k
paper capital).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Optional

from framework.backtest.shadow_log_replay import (
    ShadowExitConfig,
    load_shadow_rows,
    replay_shadow,
    summarize_shadow,
)


def _parse_dt(arg: Optional[str]) -> Optional[datetime]:
    if arg is None:
        return None
    return datetime.fromisoformat(arg).replace(tzinfo=timezone.utc)


def _print_summary(label: str, summary, n_rows: int) -> None:
    cfg = summary.config.label() if summary.config else "(no config)"
    print(f"\n=== {label} ===", file=sys.stderr)
    print(f"  Config: {cfg}", file=sys.stderr)
    print(f"  N: {summary.n}  (excluded: {summary.n_excluded} / {n_rows} loaded)", file=sys.stderr)
    print(f"  WR: {summary.wr * 100:.2f}%  ({summary.wins} W / {summary.losses} L / {summary.flat} F)", file=sys.stderr)
    print(f"  Net PnL: ${summary.net_pnl_usd:,.2f}  ({summary.net_pnl_pct:+.3f}% total)", file=sys.stderr)
    print(f"  Avg PnL: ${summary.avg_pnl_usd:,.2f} per trade  ({summary.avg_pnl_pct:+.3f}%)", file=sys.stderr)
    print(f"  Avg Win/Loss USD: ${summary.avg_win_usd:,.2f} / ${summary.avg_loss_usd:,.2f}", file=sys.stderr)
    print(f"  PnL %: median={summary.median_pnl_pct:+.2f}  p25={summary.p25_pnl_pct:+.2f}  p75={summary.p75_pnl_pct:+.2f}  min={summary.min_pnl_pct:+.2f}  max={summary.max_pnl_pct:+.2f}", file=sys.stderr)
    if summary.sharpe is not None:
        print(f"  Sharpe (annualized estimate): {summary.sharpe:.3f}", file=sys.stderr)
    if summary.by_exit_window:
        print(f"  Exit window mix: {summary.by_exit_window}", file=sys.stderr)


def _sweep(rows, notional: float) -> None:
    """Hard-coded sweep of common configs for quick comparison."""
    configs = [
        ("baseline (-8 / +30 / 12h)", ShadowExitConfig(8, 30, 12)),
        ("tighter (-5 / +15 / 4h)", ShadowExitConfig(5, 15, 4)),
        ("no_tp (-8 / none / 12h)", ShadowExitConfig(8, None, 12)),
        ("no_stop (none / +30 / 12h)", ShadowExitConfig(None, 30, 12)),
        ("pure hold 4h", ShadowExitConfig(None, None, 4)),
        ("pure hold 12h", ShadowExitConfig(None, None, 12)),
        ("wide TP -8 / +200 / 12h", ShadowExitConfig(8, 200, 12)),
        ("very wide TP -8 / +1000 / 12h", ShadowExitConfig(8, 1000, 12)),
        ("wide stop -50 / +200 / 12h", ShadowExitConfig(50, 200, 12)),
        ("optimistic tie / -8 / +200", ShadowExitConfig(8, 200, 12, tie_break="optimistic")),
    ]

    print(f"\n=== SHADOW LOG SWEEP — {len(rows)} signals, notional ${notional:,.0f} ===\n", file=sys.stderr)
    print(f"{'Config':<40} {'N':>4} {'WR':>7} {'NetPnL':>10} {'Med%':>7} {'Sharpe':>7}",
          file=sys.stderr)
    print("-" * 80, file=sys.stderr)
    for label, cfg in configs:
        _, summary = replay_shadow(rows, cfg, notional)
        s = summary.sharpe if summary.sharpe is not None else 0.0
        print(
            f"{label:<40} {summary.n:>4} {summary.wr * 100:>6.1f}% "
            f"${summary.net_pnl_usd:>8,.0f} "
            f"{summary.median_pnl_pct:>+6.2f}% {s:>+7.3f}",
            file=sys.stderr,
        )
    print(file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default=None, help="ISO datetime to filter shadow_log rows")
    parser.add_argument("--limit", type=int, default=10_000)
    parser.add_argument("--notional", type=float, default=400.0)
    parser.add_argument("--stop", type=float, default=8.0)
    parser.add_argument("--no-stop", action="store_true", help="Disable stop entirely")
    parser.add_argument("--tp", type=float, default=30.0)
    parser.add_argument("--no-tp", action="store_true", help="Disable TP entirely")
    parser.add_argument("--hold", type=float, default=12.0,
                        help="Max hold hours (snapshot windows: 0.5, 1, 4, 12)")
    parser.add_argument("--leverage", type=float, default=1.0)
    parser.add_argument("--slippage-bps", type=float, default=0.0)
    parser.add_argument("--tie-break", choices=("pessimistic", "optimistic", "mfe_first"),
                        default="pessimistic")
    parser.add_argument("--sweep", action="store_true",
                        help="Run hard-coded comparison sweep across common configs")
    parser.add_argument("--include-partial", action="store_true",
                        help="Include status='partial' rows (not just status='complete')")
    args = parser.parse_args()

    rows = load_shadow_rows(
        since=_parse_dt(args.since),
        only_complete=not args.include_partial,
        limit=args.limit,
    )
    print(f"[load] {len(rows)} shadow_log rows", file=sys.stderr)
    if not rows:
        return 0

    if args.sweep:
        _sweep(rows, args.notional)
        return 0

    cfg = ShadowExitConfig(
        stop_pct=None if args.no_stop else args.stop,
        tp_pct=None if args.no_tp else args.tp,
        hold_hours=args.hold,
        leverage=args.leverage,
        slippage_bps=args.slippage_bps,
        tie_break=args.tie_break,
    )
    _, summary = replay_shadow(rows, cfg, args.notional)
    _print_summary(f"shadow_log replay", summary, len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
