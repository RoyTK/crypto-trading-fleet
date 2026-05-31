"""Shadow-log backtest: apply configurable exit logic to copy_signal_shadow_log.

Different shape from signal_replay because we don't have the full price
series — only snapshots at +30m / +1h / +4h / +12h plus MFE/MAE within
the 12h window.

The 2026-05-30 distribution finding: COPY's cluster signal has a
positive-skew payoff (median -1%, but 7% of signals go 10x+). The
current -8/+30 exits CAP this edge. This module lets us simulate
different exit configs against the actual fired signals to find the
config that captures the tail.

Simulation approximation: given (stop_pct, tp_pct, hold_hours), use
MFE/MAE to determine if either threshold was touched within the window,
otherwise exit at the snapshot price closest to hold_hours.

Tie-break (when both stop AND TP would have fired within the window):
- 'pessimistic' (default): assume stop fired first
- 'optimistic': assume TP fired first
- 'mfe_first': MFE timestamp came earlier? (uses mfe_at/mae_at if both populated)

For positive-skew strategies the tie-break matters — running both
'pessimistic' and 'optimistic' brackets the EV honestly.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import text

from framework.db import session_scope
from framework.logging_setup import get_logger


log = get_logger(__name__)


@dataclass
class ShadowExitConfig:
    """Exit profile for shadow-log replay.

    stop_pct: |percent| from entry. None = no stop.
    tp_pct: percent from entry. None = no TP.
    hold_hours: max hold time. Choose from {0.5, 1, 4, 12} (snapshot windows).
    leverage: PnL multiplier.
    slippage_bps: round-trip slippage cost in bps.
    tie_break: 'pessimistic' | 'optimistic' | 'mfe_first'
    """
    stop_pct: Optional[float] = 8.0
    tp_pct: Optional[float] = 30.0
    hold_hours: float = 12.0
    leverage: float = 1.0
    slippage_bps: float = 0.0
    tie_break: str = "pessimistic"

    def label(self) -> str:
        stop_s = f"{self.stop_pct:.1f}" if self.stop_pct is not None else "none"
        tp_s = f"{self.tp_pct:.1f}" if self.tp_pct is not None else "none"
        return (
            f"stop={stop_s} tp={tp_s} hold={self.hold_hours:.1f}h "
            f"lev={self.leverage:.1f}x slip={self.slippage_bps:.0f}bp tie={self.tie_break}"
        )


@dataclass
class ShadowLogRow:
    id: int
    cluster_uuid: str
    token_mint: str
    cluster_size: int
    wallet_tier: str
    entry_price: float
    price_30m: Optional[float]
    price_1h: Optional[float]
    price_4h: Optional[float]
    price_12h: Optional[float]
    mfe_pct: Optional[float]
    mae_pct: Optional[float]
    mfe_at: Optional[datetime]
    mae_at: Optional[datetime]
    fired_at: datetime


@dataclass
class ShadowResult:
    cluster_uuid: str
    exit_window: str  # 'stop' | 'tp' | 'hold_30m' | 'hold_1h' | 'hold_4h' | 'hold_12h'
    exit_pct: float   # signed return percent (after slippage, before leverage)
    pnl_pct: float    # after leverage
    pnl_usd: float


@dataclass
class ShadowSummary:
    n: int
    n_excluded: int
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
    median_pnl_pct: float
    p25_pnl_pct: float
    p75_pnl_pct: float
    max_pnl_pct: float
    min_pnl_pct: float
    sharpe: Optional[float]
    by_exit_window: dict = field(default_factory=dict)
    config: Optional[ShadowExitConfig] = None


def load_shadow_rows(
    *,
    since: Optional[datetime] = None,
    only_complete: bool = True,
    limit: int = 10_000,
) -> list[ShadowLogRow]:
    """Load completed shadow_log rows for replay."""
    conditions: list[str] = []
    if only_complete:
        conditions.append("status = 'complete'")
    if since is not None:
        conditions.append("fired_at >= :since")
    conditions.append("entry_price IS NOT NULL")
    where = " AND ".join(conditions)
    sql = (
        f"SELECT id, cluster_uuid, token_mint, cluster_size, wallet_tier, "
        f"       entry_price, price_30m, price_1h, price_4h, price_12h, "
        f"       mfe_pct, mae_pct, mfe_at, mae_at, fired_at "
        f"FROM copy_signal_shadow_log WHERE {where} "
        f"ORDER BY fired_at ASC LIMIT :lim"
    )
    params: dict = {"lim": int(limit)}
    if since is not None:
        params["since"] = since
    with session_scope() as s:
        rows = s.execute(text(sql), params).all()
    return [
        ShadowLogRow(
            id=int(r.id),
            cluster_uuid=str(r.cluster_uuid),
            token_mint=str(r.token_mint),
            cluster_size=int(r.cluster_size),
            wallet_tier=str(r.wallet_tier),
            entry_price=float(r.entry_price),
            price_30m=float(r.price_30m) if r.price_30m is not None else None,
            price_1h=float(r.price_1h) if r.price_1h is not None else None,
            price_4h=float(r.price_4h) if r.price_4h is not None else None,
            price_12h=float(r.price_12h) if r.price_12h is not None else None,
            mfe_pct=float(r.mfe_pct) if r.mfe_pct is not None else None,
            mae_pct=float(r.mae_pct) if r.mae_pct is not None else None,
            mfe_at=r.mfe_at,
            mae_at=r.mae_at,
            fired_at=r.fired_at,
        )
        for r in rows
    ]


def _snapshot_for_hold(row: ShadowLogRow, hold_hours: float) -> Optional[float]:
    """Return the snapshot price corresponding to hold_hours (≤ 12)."""
    if hold_hours <= 0.5:
        return row.price_30m or row.entry_price
    if hold_hours <= 1.0:
        return row.price_1h or row.price_30m or row.entry_price
    if hold_hours <= 4.0:
        return row.price_4h or row.price_1h or row.price_30m or row.entry_price
    return row.price_12h or row.price_4h or row.price_1h or row.entry_price


def simulate_shadow_one(
    row: ShadowLogRow,
    config: ShadowExitConfig,
    notional_usd: float,
) -> Optional[ShadowResult]:
    """Simulate one shadow_log row under the given exit config."""
    entry = row.entry_price
    if entry is None or entry <= 0:
        return None

    mfe = row.mfe_pct if row.mfe_pct is not None else 0.0
    mae = row.mae_pct if row.mae_pct is not None else 0.0  # already signed (negative)

    stop_hit = config.stop_pct is not None and mae <= -config.stop_pct
    tp_hit = config.tp_pct is not None and mfe >= config.tp_pct

    exit_window: str
    exit_pct: float

    if stop_hit and tp_hit:
        # Tie-break
        if config.tie_break == "optimistic":
            exit_window = "tp"
            exit_pct = config.tp_pct
        elif config.tie_break == "mfe_first" and row.mfe_at is not None and row.mae_at is not None:
            if row.mfe_at <= row.mae_at:
                exit_window = "tp"
                exit_pct = config.tp_pct
            else:
                exit_window = "stop"
                exit_pct = -config.stop_pct
        else:
            # pessimistic default
            exit_window = "stop"
            exit_pct = -config.stop_pct
    elif stop_hit:
        exit_window = "stop"
        exit_pct = -config.stop_pct
    elif tp_hit:
        exit_window = "tp"
        exit_pct = config.tp_pct
    else:
        # Neither — use the hold-window snapshot
        snap_price = _snapshot_for_hold(row, config.hold_hours)
        if snap_price is None or snap_price <= 0:
            return None
        exit_pct = (snap_price - entry) / entry * 100.0
        # Label the hold window
        if config.hold_hours <= 0.5:
            exit_window = "hold_30m"
        elif config.hold_hours <= 1.0:
            exit_window = "hold_1h"
        elif config.hold_hours <= 4.0:
            exit_window = "hold_4h"
        else:
            exit_window = "hold_12h"

    # Slippage + leverage
    exit_pct -= config.slippage_bps / 100.0
    pnl_pct = exit_pct * config.leverage
    pnl_usd = notional_usd * (pnl_pct / 100.0)

    return ShadowResult(
        cluster_uuid=row.cluster_uuid,
        exit_window=exit_window,
        exit_pct=exit_pct,
        pnl_pct=pnl_pct,
        pnl_usd=pnl_usd,
    )


def summarize_shadow(
    results: Iterable[ShadowResult],
    *,
    n_excluded: int = 0,
    config: Optional[ShadowExitConfig] = None,
) -> ShadowSummary:
    res = list(results)
    n = len(res)
    if n == 0:
        return ShadowSummary(
            n=0, n_excluded=n_excluded, wins=0, losses=0, flat=0, wr=0.0,
            net_pnl_pct=0.0, net_pnl_usd=0.0, avg_pnl_pct=0.0, avg_pnl_usd=0.0,
            avg_win_usd=0.0, avg_loss_usd=0.0,
            median_pnl_pct=0.0, p25_pnl_pct=0.0, p75_pnl_pct=0.0,
            max_pnl_pct=0.0, min_pnl_pct=0.0,
            sharpe=None, config=config,
        )

    pnls = [r.pnl_pct for r in res]
    pnls_sorted = sorted(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    flat = n - wins - losses
    net_pct = sum(pnls)
    net_usd = sum(r.pnl_usd for r in res)

    win_pnls = [r.pnl_usd for r in res if r.pnl_usd > 0]
    loss_pnls = [r.pnl_usd for r in res if r.pnl_usd < 0]
    avg_win = (sum(win_pnls) / len(win_pnls)) if win_pnls else 0.0
    avg_loss = (sum(loss_pnls) / len(loss_pnls)) if loss_pnls else 0.0

    def _pct(p: float) -> float:
        if not pnls_sorted:
            return 0.0
        idx = max(0, min(len(pnls_sorted) - 1, int(p * (len(pnls_sorted) - 1))))
        return pnls_sorted[idx]

    sharpe: Optional[float] = None
    if n >= 5:
        mean = statistics.mean(pnls)
        sd = statistics.stdev(pnls)
        if sd > 0:
            # Approx 365 trades/yr scaling assumption (need real cadence to refine)
            sharpe = (mean / sd) * math.sqrt(365.0 / max(n, 1))

    by_window: dict[str, int] = {}
    for r in res:
        by_window[r.exit_window] = by_window.get(r.exit_window, 0) + 1

    return ShadowSummary(
        n=n, n_excluded=n_excluded, wins=wins, losses=losses, flat=flat,
        wr=round(wins / n, 4),
        net_pnl_pct=round(net_pct, 4),
        net_pnl_usd=round(net_usd, 2),
        avg_pnl_pct=round(net_pct / n, 4),
        avg_pnl_usd=round(net_usd / n, 2),
        avg_win_usd=round(avg_win, 2),
        avg_loss_usd=round(avg_loss, 2),
        median_pnl_pct=round(_pct(0.5), 4),
        p25_pnl_pct=round(_pct(0.25), 4),
        p75_pnl_pct=round(_pct(0.75), 4),
        max_pnl_pct=round(max(pnls), 4),
        min_pnl_pct=round(min(pnls), 4),
        sharpe=round(sharpe, 3) if sharpe is not None else None,
        by_exit_window=by_window,
        config=config,
    )


def replay_shadow(
    rows: list[ShadowLogRow],
    config: ShadowExitConfig,
    notional_usd: float = 400.0,
) -> tuple[list[ShadowResult], ShadowSummary]:
    results: list[ShadowResult] = []
    excluded = 0
    for r in rows:
        out = simulate_shadow_one(r, config, notional_usd)
        if out is None:
            excluded += 1
            continue
        results.append(out)
    return results, summarize_shadow(results, n_excluded=excluded, config=config)
