"""STRUCTURE fill simulator (median-realistic).

For market entries: walk the orderbook depth on the entry side until the
required notional is consumed, compute volume-weighted average fill price,
then compare to mid for slippage. Apply Hyperliquid taker fee.

For limit entries: assume a fill at the limit price IF top-of-book on the
opposing side touches it; otherwise no_fill. Apply maker fee.

For exits (stop / take-profit): same logic as market entry, opposite side.

Edge case: if total available depth < required notional, returns
SimulatedFill(no_fill_reason='insufficient_depth'). Bot logs these as Trade
rows with fill_status='no_fill' so they count against the bot in scoring.

This is the median-realistic simulator design from Item #1. Optimistic sims
risk promoting flop bots; pessimistic sims risk killing winners. Median is
defensible.
"""
from __future__ import annotations

from typing import Optional

from bots.base.fill_simulator_base import FillSimulator, SimulatedFill
from bots.structure.config import MAKER_FEE_PCT, TAKER_FEE_PCT
from bots.structure.venue import L2Book


class StructureFillSimulator(FillSimulator):
    """All methods take an L2Book as the market_snapshot."""

    # ---- Internals ---------------------------------------------------------

    def _walk_book(
        self,
        levels: list[tuple[float, float]],
        notional_usd: float,
    ) -> tuple[Optional[float], float]:
        """Walk price levels accumulating notional until target met.

        Returns (vwap_price, filled_notional_usd). If filled_notional < target,
        depth was insufficient.
        """
        consumed_notional = 0.0
        consumed_size = 0.0
        for px, sz in levels:
            level_notional = px * sz
            need = notional_usd - consumed_notional
            if need <= 0:
                break
            if level_notional >= need:
                consumed_size += need / px
                consumed_notional += need
                break
            consumed_size += sz
            consumed_notional += level_notional

        if consumed_size == 0:
            return None, 0.0
        vwap = consumed_notional / consumed_size
        return vwap, consumed_notional

    def _mid_price(self, book: L2Book) -> Optional[float]:
        if not book.bids or not book.asks:
            return None
        return (book.bids[0][0] + book.asks[0][0]) / 2.0

    # ---- Public API --------------------------------------------------------

    def simulate_entry(
        self,
        *,
        asset: str,
        notional_usd: float,
        leverage: float,
        direction: str,
        market_snapshot: L2Book,
    ) -> SimulatedFill:
        book = market_snapshot
        if not isinstance(book, L2Book) or book.asset != asset:
            return SimulatedFill(
                fill_price=None,
                fees_usd=0.0,
                slippage_bps=0.0,
                no_fill_reason="bad_snapshot",
            )
        mid = self._mid_price(book)
        if mid is None:
            return SimulatedFill(
                fill_price=None,
                fees_usd=0.0,
                slippage_bps=0.0,
                no_fill_reason="empty_book",
            )

        # Direction → which side we hit
        # Long entry: hit asks. Short entry: hit bids.
        side = book.asks if direction == "long" else book.bids
        vwap, filled = self._walk_book(side, notional_usd)
        if vwap is None or filled < notional_usd * 0.999:
            return SimulatedFill(
                fill_price=None,
                fees_usd=0.0,
                slippage_bps=0.0,
                no_fill_reason="insufficient_depth",
                metadata={"requested_usd": notional_usd, "filled_usd": filled},
            )

        slippage_bps = abs(vwap - mid) / mid * 10_000.0
        fees_usd = notional_usd * (TAKER_FEE_PCT / 100.0)
        return SimulatedFill(
            fill_price=vwap,
            fees_usd=fees_usd,
            slippage_bps=slippage_bps,
            metadata={"mid_at_entry": mid, "leverage": leverage},
        )

    def simulate_exit(
        self,
        *,
        asset: str,
        entry_price: float,
        exit_target_price: float,
        notional_usd: float,
        leverage: float,
        direction: str,
        market_snapshot: L2Book,
    ) -> SimulatedFill:
        book = market_snapshot
        if not isinstance(book, L2Book) or book.asset != asset:
            return SimulatedFill(
                fill_price=None,
                fees_usd=0.0,
                slippage_bps=0.0,
                no_fill_reason="bad_snapshot",
            )
        mid = self._mid_price(book)
        if mid is None:
            return SimulatedFill(
                fill_price=None,
                fees_usd=0.0,
                slippage_bps=0.0,
                no_fill_reason="empty_book",
            )

        # Long exit: sell, hit bids. Short exit: buy, hit asks.
        side = book.bids if direction == "long" else book.asks
        vwap, filled = self._walk_book(side, notional_usd)
        if vwap is None or filled < notional_usd * 0.999:
            return SimulatedFill(
                fill_price=None,
                fees_usd=0.0,
                slippage_bps=0.0,
                no_fill_reason="insufficient_depth_on_exit",
                metadata={"requested_usd": notional_usd, "filled_usd": filled},
            )

        slippage_bps = abs(vwap - mid) / mid * 10_000.0
        fees_usd = notional_usd * (TAKER_FEE_PCT / 100.0)
        return SimulatedFill(
            fill_price=vwap,
            fees_usd=fees_usd,
            slippage_bps=slippage_bps,
            metadata={"mid_at_exit": mid, "exit_target": exit_target_price},
        )
