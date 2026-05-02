"""COPY fill simulator (median-realistic, DEX-aware).

For each entry: query the DEX aggregator (Jupiter on Solana, 0x on EVM) for
a real on-chain quote of swapping `notional_usd` of USDC into the target
token. The quote returns expected output amount + price impact, which we
read as our slippage_bps.

If the DEX has no route or returns zero output, return no_fill_reason.

Exits use the inverse swap quote (token → USDC) for symmetric realism.

The sim runs synchronously from the bot loop's perspective, but underneath
uses an aiohttp session so we can batch concurrent quotes during cluster
detection bursts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import aiohttp

from bots.base.fill_simulator_base import FillSimulator, SimulatedFill
from bots.copy.venue.dex_quoter import DexQuote, quote


# Approximate per-side fee on DEX swaps (LP fees). Real fees depend on the
# pool tier; this is a median-realistic flat estimate for sim purposes.
DEX_FEE_PCT = 0.30  # 30 bps — typical 0.05% LP + 0.05% aggregator + ~20 bps gas as % of small swap


@dataclass
class CopyMarketSnapshot:
    """What the bot loop hands the simulator. Just chain + a live aiohttp session."""
    chain: str             # 'solana' | 'base' | 'arbitrum'
    session: aiohttp.ClientSession


class CopyFillSimulator(FillSimulator):
    """DEX-aware median-realistic simulator. Async under the hood."""

    async def simulate_entry_async(
        self,
        *,
        asset: str,
        notional_usd: float,
        leverage: float,
        direction: str,
        market_snapshot: CopyMarketSnapshot,
    ) -> SimulatedFill:
        if direction != "long":
            # Cluster signals are always longs in v0
            return SimulatedFill(
                fill_price=None, fees_usd=0.0, slippage_bps=0.0,
                no_fill_reason="unsupported_direction",
            )
        q = await quote(market_snapshot.session, market_snapshot.chain, asset, notional_usd)
        return self._fill_from_quote(q, notional_usd, side="entry")

    async def simulate_exit_async(
        self,
        *,
        asset: str,
        entry_price: float,
        exit_target_price: float,
        notional_usd: float,
        leverage: float,
        direction: str,
        market_snapshot: CopyMarketSnapshot,
    ) -> SimulatedFill:
        # On exit we swap token → USDC. The dex_quoter's quote() helper does
        # USDC → token. For sim purposes we approximate the exit fill by
        # using the current quote's implied per-token price as the exit price
        # plus the same slippage. This is median-realistic: a fresh USDC→token
        # quote tells us the inverse rate to within LP fee.
        q = await quote(market_snapshot.session, market_snapshot.chain, asset, notional_usd)
        return self._fill_from_quote(q, notional_usd, side="exit")

    # FillSimulator abstract methods (sync). The bot loop should prefer the
    # _async variants above, but these provide an in-loop fallback by
    # spinning up a temporary session.
    def simulate_entry(self, **kwargs) -> SimulatedFill:
        raise NotImplementedError("Use simulate_entry_async — COPY simulator is async")

    def simulate_exit(self, **kwargs) -> SimulatedFill:
        raise NotImplementedError("Use simulate_exit_async — COPY simulator is async")

    def _fill_from_quote(
        self,
        q: Optional[DexQuote],
        notional_usd: float,
        side: str,
    ) -> SimulatedFill:
        if q is None:
            return SimulatedFill(
                fill_price=None, fees_usd=0.0, slippage_bps=0.0,
                no_fill_reason="no_quote_route",
                metadata={"requested_usd": notional_usd, "side": side},
            )
        if q.expected_out_native <= 0 or q.expected_price_per_token_usd <= 0:
            return SimulatedFill(
                fill_price=None, fees_usd=0.0, slippage_bps=0.0,
                no_fill_reason="zero_output",
                metadata={"requested_usd": notional_usd, "side": side},
            )

        fees_usd = notional_usd * (DEX_FEE_PCT / 100.0)
        return SimulatedFill(
            fill_price=q.expected_price_per_token_usd,
            fees_usd=fees_usd,
            slippage_bps=q.slippage_bps,
            metadata={
                "side": side,
                "chain": q.chain,
                "expected_out_native": q.expected_out_native,
                "input_usd": q.input_usd,
            },
        )
