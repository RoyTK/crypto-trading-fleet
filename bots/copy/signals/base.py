"""Shared types for COPY signal generators.

Each generator returns zero or more SignalCandidate objects per evaluation.
Mirrors the pattern in bots/structure/signals/base.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SignalCandidate:
    signal_type: str            # 'cluster_buy'
    asset: str                  # token mint (Solana) or token contract (EVM)
    chain: str                  # 'solana' | 'base' | 'arbitrum'
    direction: str              # always 'long' for cluster signals in v0
    cluster_size: int = 0       # number of distinct wallets in the cluster
    payload: dict[str, Any] = field(default_factory=dict)
    take_profit_pct: Optional[float] = None
    stop_pct: Optional[float] = None
    timeout_hours: Optional[int] = None

    @property
    def venue(self) -> str:
        """Map chain → DB-side venue label (we use chain name directly)."""
        return self.chain
