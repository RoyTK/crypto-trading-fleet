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

import math
from dataclasses import dataclass
from typing import Optional

import aiohttp

from bots.base.fill_simulator_base import FillSimulator, SimulatedFill
from bots.copy.venue.dex_quoter import DexQuote, quote


# Approximate per-side fee on DEX swaps (LP fees). Real fees depend on the
# pool tier; this is a median-realistic flat estimate for sim purposes.
DEX_FEE_PCT = 0.30  # 30 bps — LEGACY flat fee (kept for callers not yet migrated). Prefer dex_fee_pct().

# Real Pump.fun/PumpSwap swap fee is ~1.25% on the bonding curve / small caps,
# tapering to 0.30% only above ~$20M mcap (verified 2026-07-18, pump.fun/docs/fees).
# The old flat 0.30% understated fees on the fresh/thin tokens we mostly trade
# (promobuy enters <1h-old tokens 86% of the time).
_FEE_BONDING_PCT = 1.25     # <= ~$85k mcap (bonding curve / brand-new)
_FEE_GRADUATED_PCT = 0.30   # >= ~$20M mcap (large, low-fee tier)
_FEE_MCAP_LO = 85_000.0
_FEE_MCAP_HI = 20_000_000.0
_LIQ_TO_MCAP = 8.0          # rough: AMM pool TVL ~ mcap/8 (proxy when mcap unknown)


def dex_fee_pct(mcap_usd: Optional[float] = None, liquidity_usd: Optional[float] = None) -> float:
    """Approximate per-side swap fee (%) for a token, by market cap. Prefers mcap;
    falls back to liquidity as a proxy (pool TVL ~ mcap/8); unknown -> bonding-curve
    (high) fee. Log-linear taper 1.25% (<= $85k) -> 0.30% (>= $20M). Replaces the flat
    DEX_FEE_PCT for fee accounting."""
    m = mcap_usd
    if (m is None or m <= 0) and liquidity_usd is not None and liquidity_usd > 0:
        m = liquidity_usd * _LIQ_TO_MCAP
    if m is None or m <= 0 or m <= _FEE_MCAP_LO:
        return _FEE_BONDING_PCT
    if m >= _FEE_MCAP_HI:
        return _FEE_GRADUATED_PCT
    t = (math.log(m) - math.log(_FEE_MCAP_LO)) / (math.log(_FEE_MCAP_HI) - math.log(_FEE_MCAP_LO))
    return _FEE_BONDING_PCT + t * (_FEE_GRADUATED_PCT - _FEE_BONDING_PCT)

# Base paper-exit slippage for a liquid token (bps); depth impact is modelled on
# top of this per fill.
EXIT_SLIPPAGE_BPS_DEFAULT = 100.0


def liquidity_aware_exit_price(
    mid: float, size_usd: float, liquidity_usd: Optional[float],
    base_slip_bps: float = EXIT_SLIPPAGE_BPS_DEFAULT,
) -> tuple[float, float]:
    """Effective paper-exit price selling `size_usd` at `mid` into a pool of
    `liquidity_usd`. Depth impact ≈ size/(liquidity+size), floored at the base
    slippage and capped near-total. `liquidity_usd is None` (unknown) → base
    slippage only; `<= 0` → total loss. Returns (effective_price, slip_fraction).

    SINGLE SOURCE for BOTH full-close (main._build_paper_exit) and partial-tier
    fills (loop_helpers.execute_paper_partial_close). Before 2026-07-18 the partial
    path skipped this entirely (raw mid + flat fee), so every ladder-tier win was
    booked with zero depth impact — the whole +$25k positive side rode the unmodeled
    path. Keep the two fill paths on this one function."""
    base = max(0.0, base_slip_bps) / 10000.0
    if liquidity_usd is None:
        frac = base
    elif liquidity_usd <= 0:
        frac = 1.0
    else:
        frac = min(0.97, max(base, size_usd / (liquidity_usd + size_usd)))
    return mid * (1.0 - frac), frac


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
