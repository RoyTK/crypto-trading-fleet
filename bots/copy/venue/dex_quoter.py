"""DEX quoter — used by the fill simulator to estimate slippage at signal time.

Solana: Jupiter aggregator quote API
EVM: 0x aggregator quote API (covers Uniswap, SushiSwap, Curve, etc. across Base + Arbitrum)

Both APIs are free (no auth needed for public quote endpoints) and return
expected output amount + price impact for a given input. We use price impact
as the slippage_bps for the simulator.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import aiohttp

from bots.copy.config import get_copy_settings
from framework.logging_setup import get_logger


log = get_logger(__name__)

# Native token mints/addresses for input quoting (we always quote a buy as
# "swap USDC → token X" since we want to know slippage on entering position X)
USDC_SOLANA_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_ARBITRUM = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"


@dataclass
class DexQuote:
    asset: str
    chain: str           # 'solana' | 'base' | 'arbitrum'
    input_usd: float
    expected_out_native: float
    expected_price_per_token_usd: float
    slippage_bps: float
    raw_response: Optional[dict] = None


async def quote_solana(
    session: aiohttp.ClientSession,
    output_mint: str,
    input_usd: float,
    slippage_bps_tolerance: int = 200,
) -> Optional[DexQuote]:
    """Solana token quote: try Jupiter first, fall back to Birdeye price.

    Jupiter has accurate route + price-impact for tokens with established
    DEX pools (Raydium, Orca, etc.) — but doesn't cover pre-graduation
    Pump.fun bonding-curve tokens. Birdeye covers virtually all Solana
    tokens via their price oracle.

    Returns None only when neither has data.
    """
    q = await _quote_solana_jupiter(session, output_mint, input_usd, slippage_bps_tolerance)
    if q is not None:
        return q
    return await _quote_solana_birdeye(session, output_mint, input_usd)


async def _quote_solana_jupiter(
    session: aiohttp.ClientSession,
    output_mint: str,
    input_usd: float,
    slippage_bps_tolerance: int,
) -> Optional[DexQuote]:
    settings = get_copy_settings()
    amount_in = int(input_usd * 1_000_000)  # USDC has 6 decimals
    params = {
        "inputMint": USDC_SOLANA_MINT,
        "outputMint": output_mint,
        "amount": str(amount_in),
        "slippageBps": str(slippage_bps_tolerance),
    }
    try:
        async with session.get(settings.jupiter_quote_url, params=params,
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except Exception:
        return None

    try:
        out_amount = int(data.get("outAmount", 0))
        if out_amount == 0:
            return None
        price_impact_pct = float(data.get("priceImpactPct", 0))
        slippage_bps = abs(price_impact_pct) * 100.0
        return DexQuote(
            asset=output_mint,
            chain="solana",
            input_usd=input_usd,
            expected_out_native=float(out_amount),
            expected_price_per_token_usd=input_usd / out_amount if out_amount > 0 else 0,
            slippage_bps=slippage_bps,
            raw_response=data,
        )
    except Exception:
        log.exception("jupiter_quote_parse_failed", mint=output_mint)
        return None


async def _quote_solana_birdeye(
    session: aiohttp.ClientSession,
    output_mint: str,
    input_usd: float,
) -> Optional[DexQuote]:
    """Birdeye /defi/price fallback. Covers Pump.fun pre-graduation tokens.

    Returns DexQuote with a flat slippage estimate (no liquidity-aware
    impact calc — Birdeye's price endpoint doesn't expose pool depth).
    100 bps is a reasonable median for memecoin liquidity.
    """
    settings = get_copy_settings()
    if not settings.birdeye_api_key:
        log.warning("birdeye_no_api_key", mint=output_mint)
        return None
    url = "https://public-api.birdeye.so/defi/price"
    params = {"address": output_mint}
    headers = {"X-API-KEY": settings.birdeye_api_key, "x-chain": "solana"}
    try:
        async with session.get(url, params=params, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                log.warning("birdeye_price_failed", status=r.status, mint=output_mint)
                return None
            data = await r.json()
    except Exception:
        log.exception("birdeye_price_exception", mint=output_mint)
        return None

    try:
        price_per_token_usd = float(((data.get("data") or {}).get("value")) or 0)
        if price_per_token_usd <= 0:
            return None
        # No liquidity data → flat 100 bps slippage estimate for memecoins
        ESTIMATED_SLIPPAGE_BPS = 100.0
        # Output amount in token native units. We don't know token decimals
        # without an extra call; consumer (sim) only uses price_per_token_usd
        # so leave expected_out_native as USD/price for downstream logging.
        return DexQuote(
            asset=output_mint,
            chain="solana",
            input_usd=input_usd,
            expected_out_native=input_usd / price_per_token_usd,
            expected_price_per_token_usd=price_per_token_usd,
            slippage_bps=ESTIMATED_SLIPPAGE_BPS,
            raw_response=data,
        )
    except Exception:
        log.exception("birdeye_price_parse_failed", mint=output_mint)
        return None


async def quote_evm(
    session: aiohttp.ClientSession,
    chain: str,
    output_token: str,
    input_usd: float,
    slippage_bps_tolerance: int = 200,
) -> Optional[DexQuote]:
    """Get a 0x quote for swapping USDC → output_token on `chain` (base|arbitrum).

    Returns None on failure.
    """
    settings = get_copy_settings()
    if chain == "base":
        usdc = USDC_BASE
        # 0x supports per-chain prefix or chain_id query
        url_prefix = "/swap/v1/quote"
        chain_id = 8453
    elif chain == "arbitrum":
        usdc = USDC_ARBITRUM
        url_prefix = "/swap/v1/quote"
        chain_id = 42161
    else:
        log.warning("dex_quote_unknown_chain", chain=chain)
        return None

    # USDC has 6 decimals on Base + Arbitrum
    amount_in = int(input_usd * 1_000_000)
    params = {
        "sellToken": usdc,
        "buyToken": output_token,
        "sellAmount": str(amount_in),
        "slippagePercentage": f"{slippage_bps_tolerance / 10000.0}",
        "chainId": str(chain_id),
    }
    try:
        async with session.get(
            f"{settings.zeroex_api_base}{url_prefix}", params=params,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status != 200:
                log.warning("zeroex_quote_failed", status=r.status, chain=chain, token=output_token)
                return None
            data = await r.json()
    except Exception:
        log.exception("zeroex_quote_exception", chain=chain, token=output_token)
        return None

    try:
        buy_amount = int(data.get("buyAmount", 0))
        if buy_amount == 0:
            return None
        price = float(data.get("price", 0))
        # 0x doesn't expose priceImpactPct directly; estimate from
        # guaranteedPrice vs price spread
        guaranteed = float(data.get("guaranteedPrice", price) or price)
        if price > 0:
            slippage_bps = abs(price - guaranteed) / price * 10_000
        else:
            slippage_bps = 0.0
        return DexQuote(
            asset=output_token,
            chain=chain,
            input_usd=input_usd,
            expected_out_native=float(buy_amount),
            expected_price_per_token_usd=1.0 / price if price > 0 else 0,
            slippage_bps=slippage_bps,
            raw_response=data,
        )
    except Exception:
        log.exception("zeroex_quote_parse_failed", chain=chain, token=output_token)
        return None


async def quote(
    session: aiohttp.ClientSession,
    chain: str,
    output_token: str,
    input_usd: float,
) -> Optional[DexQuote]:
    """Unified quote dispatcher by chain."""
    if chain == "solana":
        return await quote_solana(session, output_token, input_usd)
    if chain in ("base", "arbitrum"):
        return await quote_evm(session, chain, output_token, input_usd)
    log.warning("quote_unsupported_chain", chain=chain)
    return None
