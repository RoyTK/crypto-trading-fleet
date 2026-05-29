"""CLI: run signal-replay backtest with a configurable exit profile.

Examples:

  # Sweep funding_fade with tighter TP
  docker compose exec bot_structure python -m scripts.run_backtest_signals \\
    --bot structure --signal-type funding_fade \\
    --since 2026-05-01 --stop 5 --tp 10 --timeout 24 --leverage 2

  # Sweep COPY cluster_buy with inverted exits (test the H2 hypothesis)
  docker compose exec bot_copy python -m scripts.run_backtest_signals \\
    --bot copy --signal-type cluster_buy \\
    --since 2026-05-25 --stop 25 --tp 8 --timeout 12

For STRUCTURE signals: uses Hyperliquid candle history via the bot's existing
HyperliquidVenue. For COPY signals: uses Birdeye historical price.

Exit price is sampled at the candle close that contains the timeout/stop/TP
event. Coarser than tick-level — sufficient for parameter sweeps but not
for execution backtesting.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

from framework.backtest.signal_replay import (
    BacktestSummary,
    ExitConfig,
    SignalRow,
    load_signals,
    replay,
)


def _parse_dt(arg: Optional[str]) -> Optional[datetime]:
    if arg is None:
        return None
    try:
        return datetime.fromisoformat(arg).replace(tzinfo=timezone.utc)
    except ValueError:
        # Try YYYY-MM-DD
        return datetime.strptime(arg, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _hl_price_fetcher_factory():
    """Returns a price fetcher backed by Hyperliquid's candle API."""
    from bots.structure.venue import HyperliquidVenue
    venue = HyperliquidVenue()

    def fetch(asset: str, start_ts: datetime, hours: float) -> list[tuple[datetime, float]]:
        end_ts = start_ts + timedelta(hours=hours)
        # HL info.candles_snapshot signature: (asset, interval, startTime_ms, endTime_ms)
        candles = venue.info.candles_snapshot(
            asset, "5m",
            int(start_ts.timestamp() * 1000),
            int(end_ts.timestamp() * 1000),
        )
        out: list[tuple[datetime, float]] = []
        for c in candles or []:
            # Each candle: {"t": startTime_ms, "T": endTime_ms, "c": "close", ...}
            try:
                ts = datetime.fromtimestamp(int(c["t"]) / 1000.0, tz=timezone.utc)
                px = float(c.get("c") or c.get("close") or 0.0)
                if px > 0:
                    out.append((ts, px))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    return fetch


def _birdeye_price_fetcher_factory():
    """Returns a price fetcher backed by Birdeye history_price.
    Requires `BIRDEYE_API_KEY` env var.
    """
    import os
    import urllib.request
    import json as _json

    key = os.environ.get("BIRDEYE_API_KEY")
    if not key:
        raise RuntimeError("BIRDEYE_API_KEY not set in container env")

    def fetch(token_mint: str, start_ts: datetime, hours: float) -> list[tuple[datetime, float]]:
        end_ts = start_ts + timedelta(hours=hours)
        url = (
            f"https://public-api.birdeye.so/defi/history_price"
            f"?address={token_mint}&address_type=token&type=5m"
            f"&time_from={int(start_ts.timestamp())}&time_to={int(end_ts.timestamp())}"
        )
        req = urllib.request.Request(url, headers={
            "X-API-KEY": key, "x-chain": "solana", "Accept": "application/json"
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            data = _json.loads(r.read().decode())
        items = (data.get("data") or {}).get("items") or []
        out: list[tuple[datetime, float]] = []
        for it in items:
            try:
                ts = datetime.fromtimestamp(int(it["unixTime"]), tz=timezone.utc)
                px = float(it["value"])
                if px > 0:
                    out.append((ts, px))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    return fetch


def _print_summary(summary: BacktestSummary, label: str = "") -> None:
    cfg = summary.config.label() if summary.config else "(no config)"
    print(f"\n=== Backtest summary {label} ===", file=sys.stderr)
    print(f"  Config: {cfg}", file=sys.stderr)
    print(f"  N: {summary.n}  (excluded: {summary.n_excluded})", file=sys.stderr)
    print(f"  WR: {summary.wr * 100:.2f}%  ({summary.wins} W / {summary.losses} L / {summary.flat} F)", file=sys.stderr)
    print(f"  Net PnL: ${summary.net_pnl_usd:,.2f}  ({summary.net_pnl_pct:+.3f}% total)", file=sys.stderr)
    print(f"  Avg PnL: ${summary.avg_pnl_usd:,.2f} per trade  ({summary.avg_pnl_pct:+.3f}%)", file=sys.stderr)
    print(f"  Avg Win: ${summary.avg_win_usd:,.2f}  |  Avg Loss: ${summary.avg_loss_usd:,.2f}", file=sys.stderr)
    if summary.sharpe is not None:
        print(f"  Sharpe (annualized estimate): {summary.sharpe:.3f}", file=sys.stderr)
    if summary.by_exit_reason:
        print(f"  Exit reasons: {summary.by_exit_reason}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", required=True, choices=["structure", "copy"])
    parser.add_argument("--signal-type", default=None,
                        help="Filter by signal_type (e.g. funding_fade, cluster_buy)")
    parser.add_argument("--since", default=None, help="ISO date YYYY-MM-DD")
    parser.add_argument("--until", default=None)
    parser.add_argument("--limit", type=int, default=10_000)
    parser.add_argument("--stop", type=float, default=8.0, help="Stop loss %")
    parser.add_argument("--tp", type=float, default=30.0, help="Take profit %")
    parser.add_argument("--timeout", type=float, default=12.0, help="Timeout hours")
    parser.add_argument("--leverage", type=float, default=1.0)
    parser.add_argument("--slippage-bps", type=float, default=0.0)
    parser.add_argument("--notional", type=float, default=1_000.0)
    parser.add_argument("--csv", default=None, help="Output per-trade CSV path")
    args = parser.parse_args()

    config = ExitConfig(
        stop_pct=args.stop, take_profit_pct=args.tp,
        timeout_hours=args.timeout, leverage=args.leverage,
        slippage_bps=args.slippage_bps,
    )

    signals = load_signals(
        bot_id=args.bot, signal_type=args.signal_type,
        since=_parse_dt(args.since), until=_parse_dt(args.until),
        limit=args.limit,
    )
    print(f"[load] {len(signals)} signals matching filter", file=sys.stderr)
    if not signals:
        return 0

    fetcher = (
        _hl_price_fetcher_factory()
        if args.bot == "structure"
        else _birdeye_price_fetcher_factory()
    )
    results, summary = replay(signals, fetcher, config, notional_usd=args.notional)
    _print_summary(summary, label=f"({args.bot}/{args.signal_type or 'all'})")

    if args.csv:
        with open(args.csv, "w", encoding="utf-8") as fh:
            fh.write("signal_id,asset,direction,entry_at,entry_price,exit_at,exit_price,exit_reason,pnl_pct,pnl_usd\n")
            for r in results:
                fh.write(
                    f"{r.signal_id},{r.asset},{r.direction},"
                    f"{r.entry_at.isoformat()},{r.entry_price},"
                    f"{r.exit_at.isoformat()},{r.exit_price},"
                    f"{r.exit_reason},{r.pnl_pct:.4f},{r.pnl_usd:.4f}\n"
                )
        print(f"[csv] wrote {len(results)} rows → {args.csv}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
