"""Signal replay backtest — apply configurable exit logic to historical signals.

Workflow:
1. Load signals matching filter (bot_id, signal_type, date range)
2. For each signal, fetch the asset's price series from the time of
   signal forward (using HL candles for STRUCTURE, Birdeye history for
   COPY — caller injects a price-series fetcher).
3. Apply the ExitConfig (stop_pct, take_profit_pct, timeout_hours,
   leverage) to determine simulated exit price + reason.
4. Compute simulated PnL per signal + aggregate stats.

Returns a BacktestResult per signal + an aggregate summary.

Pure-data — no live API calls in the test harness; the price-series
fetcher is injected so the harness can be tested with synthetic data.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from sqlalchemy import text

from framework.db import session_scope
from framework.logging_setup import get_logger


log = get_logger(__name__)


@dataclass
class ExitConfig:
    """Exit logic parameters — these are what we sweep across in backtests."""
    stop_pct: float = 8.0           # |percent| from entry — long: entry × (1-stop/100)
    take_profit_pct: float = 30.0   # percent from entry — long: entry × (1+tp/100)
    timeout_hours: float = 12.0     # if neither hit, exit at this time
    leverage: float = 1.0           # PnL multiplier
    slippage_bps: float = 0.0       # round-trip slippage cost in bps

    def label(self) -> str:
        return (
            f"stop={self.stop_pct:.1f} tp={self.take_profit_pct:.1f} "
            f"to={self.timeout_hours:.1f}h lev={self.leverage:.1f}x slip={self.slippage_bps:.0f}bp"
        )


@dataclass
class SignalRow:
    """Minimal signal info needed to backtest."""
    id: int
    bot_id: str
    signal_type: str
    asset: str
    venue: str
    direction: str  # 'long' or 'short'
    created_at: datetime


@dataclass
class TradeResult:
    """Outcome of simulating one signal under one exit config."""
    signal_id: int
    asset: str
    direction: str
    entry_at: datetime
    entry_price: float
    exit_at: datetime
    exit_price: float
    exit_reason: str  # 'stop' | 'tp' | 'timeout' | 'no_data'
    pnl_pct: float    # leveraged
    notional_usd: float
    pnl_usd: float


@dataclass
class BacktestSummary:
    """Aggregate stats over a list of TradeResults."""
    n: int
    n_excluded: int   # signals with no price data
    wins: int
    losses: int
    flat: int
    wr: float
    net_pnl_pct: float
    net_pnl_usd: float
    avg_pnl_pct: float
    avg_pnl_usd: float
    avg_win_usd: float
    avg_loss_usd: float
    sharpe: Optional[float]   # naive per-trade Sharpe × sqrt(trades_per_year)
    by_exit_reason: dict[str, int] = field(default_factory=dict)
    config: Optional[ExitConfig] = None


# Type alias for the price-series fetcher.
# Returns a list of (ts: datetime, price: float) for the asset, starting at
# `from_ts` and covering at least `hours` afterwards. Order: ascending by time.
PriceFetcher = Callable[[str, datetime, float], list[tuple[datetime, float]]]


def load_signals(
    *,
    bot_id: str,
    signal_type: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = 10_000,
) -> list[SignalRow]:
    """Load matching signals from the `signals` table."""
    conditions = ["bot_id = :bot_id"]
    params: dict = {"bot_id": bot_id}
    if signal_type:
        conditions.append("signal_type = :st")
        params["st"] = signal_type
    if since is not None:
        conditions.append("created_at >= :since")
        params["since"] = since
    if until is not None:
        conditions.append("created_at <= :until")
        params["until"] = until
    where = " AND ".join(conditions)
    sql = (
        f"SELECT id, bot_id, signal_type, asset, venue, direction, created_at "
        f"FROM signals WHERE {where} ORDER BY created_at ASC LIMIT :lim"
    )
    params["lim"] = int(limit)
    with session_scope() as s:
        rows = s.execute(text(sql), params).all()
    return [
        SignalRow(
            id=int(r.id),
            bot_id=r.bot_id,
            signal_type=r.signal_type,
            asset=r.asset,
            venue=r.venue,
            direction=r.direction,
            created_at=r.created_at,
        )
        for r in rows
    ]


def simulate_one(
    signal: SignalRow,
    price_series: list[tuple[datetime, float]],
    config: ExitConfig,
    notional_usd: float,
) -> Optional[TradeResult]:
    """Simulate a single signal's outcome under config + price series.

    Returns None if entry price is missing or invalid.
    """
    if not price_series:
        return None
    entry_at, entry_price = price_series[0]
    if entry_price is None or entry_price <= 0:
        return None

    if signal.direction == "long":
        stop_price = entry_price * (1.0 - config.stop_pct / 100.0)
        tp_price = entry_price * (1.0 + config.take_profit_pct / 100.0)
    else:
        stop_price = entry_price * (1.0 + config.stop_pct / 100.0)
        tp_price = entry_price * (1.0 - config.take_profit_pct / 100.0)

    timeout_at = entry_at + timedelta(hours=config.timeout_hours)
    exit_reason = "timeout"
    exit_at = timeout_at
    exit_price = entry_price  # fallback if we run out of data

    # Walk forward
    for ts, px in price_series[1:]:
        if px is None or px <= 0:
            continue
        if ts > timeout_at:
            # Use last seen price at or just before timeout
            exit_reason = "timeout"
            exit_at = timeout_at
            exit_price = px
            break
        if signal.direction == "long":
            if px <= stop_price:
                exit_reason, exit_at, exit_price = "stop", ts, px
                break
            if px >= tp_price:
                exit_reason, exit_at, exit_price = "tp", ts, px
                break
        else:
            if px >= stop_price:
                exit_reason, exit_at, exit_price = "stop", ts, px
                break
            if px <= tp_price:
                exit_reason, exit_at, exit_price = "tp", ts, px
                break
    else:
        # Ran off the end of the series — use last available price
        last_ts, last_px = price_series[-1]
        if last_px is not None and last_px > 0:
            exit_at = last_ts
            exit_price = last_px

    # PnL with leverage
    raw_pct = (exit_price - entry_price) / entry_price * 100.0
    if signal.direction == "short":
        raw_pct = -raw_pct
    raw_pct -= config.slippage_bps / 100.0  # bps → percent
    pnl_pct = raw_pct * config.leverage
    pnl_usd = notional_usd * (pnl_pct / 100.0)

    return TradeResult(
        signal_id=signal.id,
        asset=signal.asset,
        direction=signal.direction,
        entry_at=entry_at,
        entry_price=entry_price,
        exit_at=exit_at,
        exit_price=exit_price,
        exit_reason=exit_reason,
        pnl_pct=pnl_pct,
        notional_usd=notional_usd,
        pnl_usd=pnl_usd,
    )


def summarize(
    results: Iterable[TradeResult],
    *,
    n_excluded: int = 0,
    config: Optional[ExitConfig] = None,
    trades_per_year_estimate: float = 365.0,
) -> BacktestSummary:
    """Aggregate stats."""
    res = list(results)
    n = len(res)
    if n == 0:
        return BacktestSummary(
            n=0, n_excluded=n_excluded, wins=0, losses=0, flat=0,
            wr=0.0, net_pnl_pct=0.0, net_pnl_usd=0.0,
            avg_pnl_pct=0.0, avg_pnl_usd=0.0,
            avg_win_usd=0.0, avg_loss_usd=0.0, sharpe=None, config=config,
        )

    wins = sum(1 for r in res if r.pnl_usd > 0)
    losses = sum(1 for r in res if r.pnl_usd < 0)
    flat = n - wins - losses
    wr = wins / n
    net_pct = sum(r.pnl_pct for r in res)
    net_usd = sum(r.pnl_usd for r in res)
    avg_pct = net_pct / n
    avg_usd = net_usd / n
    win_pnls = [r.pnl_usd for r in res if r.pnl_usd > 0]
    loss_pnls = [r.pnl_usd for r in res if r.pnl_usd < 0]
    avg_win = (sum(win_pnls) / len(win_pnls)) if win_pnls else 0.0
    avg_loss = (sum(loss_pnls) / len(loss_pnls)) if loss_pnls else 0.0

    sharpe: Optional[float] = None
    if n >= 5:
        pnls = [r.pnl_pct for r in res]
        mean = statistics.mean(pnls)
        sd = statistics.stdev(pnls)
        if sd > 0:
            sharpe = (mean / sd) * math.sqrt(trades_per_year_estimate / max(n, 1))

    by_reason: dict[str, int] = {}
    for r in res:
        by_reason[r.exit_reason] = by_reason.get(r.exit_reason, 0) + 1

    return BacktestSummary(
        n=n, n_excluded=n_excluded, wins=wins, losses=losses, flat=flat,
        wr=round(wr, 4),
        net_pnl_pct=round(net_pct, 4),
        net_pnl_usd=round(net_usd, 2),
        avg_pnl_pct=round(avg_pct, 4),
        avg_pnl_usd=round(avg_usd, 2),
        avg_win_usd=round(avg_win, 2),
        avg_loss_usd=round(avg_loss, 2),
        sharpe=round(sharpe, 3) if sharpe is not None else None,
        by_exit_reason=by_reason,
        config=config,
    )


def replay(
    signals: list[SignalRow],
    fetcher: PriceFetcher,
    config: ExitConfig,
    notional_usd: float = 1_000.0,
    *,
    fetch_hours: float = 24.0,
) -> tuple[list[TradeResult], BacktestSummary]:
    """Run signal replay end-to-end.

    fetcher(asset, start_ts, hours) → [(ts, price), ...]
    """
    results: list[TradeResult] = []
    excluded = 0
    for sig in signals:
        try:
            series = fetcher(sig.asset, sig.created_at,
                             max(fetch_hours, config.timeout_hours + 1.0))
        except Exception as e:
            log.warning("backtest_fetch_failed", asset=sig.asset,
                        signal_id=sig.id,
                        err_type=type(e).__name__, err_msg=str(e)[:300])
            excluded += 1
            continue
        out = simulate_one(sig, series, config, notional_usd)
        if out is None:
            excluded += 1
            continue
        results.append(out)
    summary = summarize(results, n_excluded=excluded, config=config)
    return results, summary
