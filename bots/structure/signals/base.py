"""Shared types for STRUCTURE signal generators.

Each generator returns zero or more SignalCandidate objects per evaluation.
The bot loop persists these to the `signals` table, runs the simulator on
them, and writes paper Trade rows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SignalCandidate:
    signal_type: str            # 'funding_fade' | 'liquidation_cascade' | 'whale_flip'
    asset: str
    direction: str              # 'long' or 'short'
    venue: str = "hyperliquid"
    conviction: float = 0.5     # 0.0..1.0; nudges sizing within band
    payload: dict[str, Any] = field(default_factory=dict)
    take_profit_pct: Optional[float] = None
    stop_pct: Optional[float] = None
    timeout_hours: Optional[int] = None
