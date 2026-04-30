"""Fill simulator interface.

Each bot's simulator returns a SimulatedFill given a Signal and a market
snapshot. Implementations should be median-realistic (no optimism, no
pessimism). They MUST log no-fill cases — these count against the bot in
scoring; the simulator never gives a free pass.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class SimulatedFill:
    fill_price: Optional[float]     # None when no_fill_reason is set; price the order filled at
    fees_usd: float
    slippage_bps: float
    no_fill_reason: Optional[str] = None  # 'insufficient_depth', 'rejected', etc.
    metadata: Optional[dict[str, Any]] = None


class FillSimulator(ABC):
    @abstractmethod
    def simulate_entry(
        self,
        *,
        asset: str,
        notional_usd: float,
        leverage: float,
        direction: str,  # 'long' or 'short'
        market_snapshot: Any,
    ) -> SimulatedFill:
        ...

    @abstractmethod
    def simulate_exit(
        self,
        *,
        asset: str,
        entry_price: float,
        exit_target_price: float,
        notional_usd: float,
        leverage: float,
        direction: str,
        market_snapshot: Any,
    ) -> SimulatedFill:
        ...
