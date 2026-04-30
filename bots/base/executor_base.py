"""Executor interface.

An executor takes a Signal + SimulatedFill and writes the corresponding Trade
row(s) to Postgres. Three modes:

- `paper`  : write Trade with mode='paper' using SimulatedFill values
- `shadow` : place a small ($5-20) real order on the venue, write Trade with
             mode='shadow', pair into calibration_records
- `live`   : place a real position-sized order, write Trade with mode='live'

For Phase 1 (Build A) only `paper` is wired; `shadow` lands in Build B; `live`
is gated behind successful PromotionScore evaluation in Phase 5.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from bots.base.fill_simulator_base import SimulatedFill


class Executor(ABC):
    @abstractmethod
    def place_paper(self, signal_id: int, sim_fill: SimulatedFill) -> Optional[int]:
        """Write a paper Trade row from sim_fill. Returns trade_id or None on no-fill."""

    @abstractmethod
    def place_shadow(self, signal_id: int, sim_fill: SimulatedFill) -> Optional[int]:
        """Place a real micro-order ($5-20) and write a shadow Trade row.

        Pairs into calibration_records with the prior paper Trade for the same
        signal. Build B only — Build A returns None.
        """

    @abstractmethod
    def close_paper(
        self,
        trade_id: int,
        exit_price: float,
        exit_reason: str,
        sim_fill: SimulatedFill,
    ) -> None:
        """Mark a paper Trade closed. Updates row in-place; doesn't insert new row."""
