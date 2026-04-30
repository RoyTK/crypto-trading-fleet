"""STRUCTURE position sizing.

Pure function — no DB, no network. Tested in isolation.

Inputs:
- signal_type: 'funding_fade' | 'liquidation_cascade' | 'whale_flip'
- paper_capital_usd: bot's notional balance
- current_dd_today_pct: positive number representing TODAY's drawdown
                       (0 = no drawdown, 15 = at-the-halt)
- conviction: 0.0..1.0 — signal-specific confidence; nudges within band

Output: (notional_usd, leverage)

Rules:
- Position size scales linearly between size_pct_min and size_pct_max by conviction
- Capped at PER_TRADE_NOTIONAL_CAP_PCT (15%) of paper_capital pre-leverage
- DD discount: as drawdown approaches the daily halt, size shrinks to 0.5x floor
"""
from __future__ import annotations

from bots.structure.config import (
    PER_TRADE_NOTIONAL_CAP_PCT,
    SIGNAL_SPECS,
    get_structure_settings,
)


def size_position(
    signal_type: str,
    paper_capital_usd: float,
    current_dd_today_pct: float = 0.0,
    conviction: float = 0.5,
) -> tuple[float, float]:
    spec = SIGNAL_SPECS.get(signal_type)
    if spec is None:
        raise ValueError(f"unknown signal_type: {signal_type}")

    conviction = max(0.0, min(1.0, conviction))
    base_pct = spec.size_pct_min + (spec.size_pct_max - spec.size_pct_min) * conviction

    notional_usd = paper_capital_usd * (base_pct / 100.0)

    # Cap pre-leverage notional at 15% of paper capital
    cap_usd = paper_capital_usd * (PER_TRADE_NOTIONAL_CAP_PCT / 100.0)
    notional_usd = min(notional_usd, cap_usd)

    # Drawdown discount: linear shrink from 1.0 (no DD) to 0.5 (at halt)
    settings = get_structure_settings()
    dd_halt_pct = settings.structure_dd_daily_pct
    if current_dd_today_pct > 0 and dd_halt_pct > 0:
        dd_ratio = min(current_dd_today_pct / dd_halt_pct, 1.0)
        discount = max(0.5, 1.0 - dd_ratio * 0.5)
        notional_usd *= discount

    return notional_usd, spec.leverage
