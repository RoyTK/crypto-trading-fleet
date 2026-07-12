"""DEX quoter — used by the fill simulator to estimate slippage at signal time.

Solana: Jupiter aggregator quote API
EVM: 0x aggregator quote API (covers Uniswap, SushiSwap, Curve, etc. across Base + Arbitrum)

Both APIs are free (no auth needed for public quote endpoints) and return
expected output amount + price impact for a given input. We use price impact
as the slippage_bps for the simulator.
"""
from __future__ import annotations

import asyncio
import time
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
    """Get Jupiter slippage estimate, but price comes from Birdeye.

    BUG FIX 2026-06-09: the previous implementation computed
    `expected_price_per_token_usd = input_usd / out_amount` where
    `out_amount` is RAW ATOMIC UNITS (token quantity × 10^decimals).
    That produces a price-per-atomic-unit, NOT a price-per-UI-token.
    For a typical 6-decimal Solana memecoin the stored entry_price
    came out 1,000,000× too small. The bug only manifested when
    Jupiter quote actually succeeded — before 2026-06-09 the
    quote-api.jup.ag endpoint was DNS-flaky, so Birdeye fallback was
    always hit (correct price). After we migrated to lite-api.jup.ag
    on 2026-06-08, Jupiter quotes started succeeding for graduated
    tokens, and tiny entry_prices started landing in trade rows.

    Result: 6 paper trades on 2026-06-09 morning showed peak_pct of
    100,000,000%+ and pseudo-PnL of $400M each, because the new
    partial-exit ladder fired all 4 tiers in one cycle on price
    ratios that LOOKED like 1,000,000× pumps but were actually just
    the scale bug.

    Fix: Jupiter doesn't return token decimals in its quote response,
    so we can't fix the math without an extra Solana RPC call per
    quote. Instead, look up the actual price via Birdeye and return
    Jupiter's slippage estimate combined with Birdeye's price. Two
    HTTP calls per quote but accurate.

    If Birdeye lookup fails, return None — caller falls through to
    `_quote_solana_birdeye` which is the pure Birdeye path. Worst-case
    behavior: paper trades use flat 100bps slippage estimate instead
    of Jupiter's price-impact-derived estimate. Acceptable for now.
    """
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
        # Floor at the configured paper-slippage minimum. Jupiter's
        # priceImpactPct is unrealistically optimistic for fresh-mint
        # memecoins (~3bps); modeling near-frictionless fills biases paper
        # PnL high. See config.copy_min_paper_slippage_bps.
        slippage_bps = max(
            get_copy_settings().copy_min_paper_slippage_bps,
            abs(price_impact_pct) * 100.0,
        )
    except Exception:
        log.exception("jupiter_quote_parse_failed", mint=output_mint)
        return None

    # Get the actual per-UI-token USD price from Birdeye, not the
    # broken atomic-unit math from Jupiter.
    settings = get_copy_settings()
    if not settings.birdeye_api_key:
        return None    # No fallback price available; caller goes to _quote_solana_birdeye
    headers = {"X-API-KEY": settings.birdeye_api_key, "x-chain": "solana"}
    try:
        async with session.get(
            "https://public-api.birdeye.so/defi/price",
            params={"address": output_mint},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return None
            body = await r.json()
    except Exception:
        return None
    try:
        price_per_token_usd = float(((body.get("data") or {}).get("value")) or 0)
    except (TypeError, ValueError):
        return None
    if price_per_token_usd <= 0:
        return None

    return DexQuote(
        asset=output_mint,
        chain="solana",
        input_usd=input_usd,
        # expected_out_native is preserved for telemetry but the price
        # comes from Birdeye, not from the broken Jupiter math.
        expected_out_native=float(out_amount),
        expected_price_per_token_usd=price_per_token_usd,
        slippage_bps=slippage_bps,
        raw_response=data,
    )


async def multi_price_solana(
    session: aiohttp.ClientSession,
    mints: list[str],
) -> dict[str, float]:
    """Batch price lookup for many Solana mints in one HTTP call.

    The single-token /defi/price endpoint costs 3 CUs per call; managing N
    open positions every 60s burns the free-tier monthly quota in ~14h at
    N=12. /defi/multi_price returns prices for up to 100 mints in a single
    request at much lower amortized CU cost.

    Returns: dict of mint -> price_per_token_usd. Missing/unsupported mints
    are simply omitted (callers distinguish "no price" from "price = 0").
    """
    if not mints:
        return {}
    settings = get_copy_settings()
    if not settings.birdeye_api_key:
        log.warning("birdeye_no_api_key", n_mints=len(mints))
        return {}
    out: dict[str, float] = {}
    headers = {"X-API-KEY": settings.birdeye_api_key, "x-chain": "solana"}
    # Birdeye caps each request at 100 addresses; chunk defensively.
    for i in range(0, len(mints), 100):
        chunk = mints[i:i + 100]
        params = {"list_address": ",".join(chunk)}
        try:
            async with session.get(
                "https://public-api.birdeye.so/defi/multi_price",
                params=params, headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    log.warning("birdeye_multi_price_failed",
                                status=r.status, n_mints=len(chunk))
                    continue
                body = await r.json()
        except Exception:
            log.exception("birdeye_multi_price_exception", n_mints=len(chunk))
            continue
        # Response shape: {"success": true, "data": {"<mint>": {"value": <price>, ...} | null, ...}}
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            continue
        for mint, entry in data.items():
            if not isinstance(entry, dict):
                continue
            try:
                price = float(entry.get("value") or 0.0)
            except (TypeError, ValueError):
                continue
            if price > 0:
                out[mint] = price
    return out


async def multi_price_liq_solana(
    session: aiohttp.ClientSession,
    mints: list[str],
) -> dict[str, dict]:
    """Batch price + CURRENT liquidity for many Solana mints in one HTTP call.

    Same endpoint/CU cost as multi_price_solana but with include_liquidity=true,
    so the position manager gets price AND live liquidity without a second call
    (the liquidity-momentum stop, 2026-06-28). Returns mint -> {"price": float,
    "liquidity": float|None}; missing/unsupported mints omitted.
    """
    if not mints:
        return {}
    settings = get_copy_settings()
    if not settings.birdeye_api_key:
        log.warning("birdeye_no_api_key", n_mints=len(mints))
        return {}
    out: dict[str, dict] = {}
    headers = {"X-API-KEY": settings.birdeye_api_key, "x-chain": "solana"}
    for i in range(0, len(mints), 100):
        chunk = mints[i:i + 100]
        params = {"list_address": ",".join(chunk), "include_liquidity": "true"}
        try:
            async with session.get(
                "https://public-api.birdeye.so/defi/multi_price",
                params=params, headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    log.warning("birdeye_multi_price_liq_failed",
                                status=r.status, n_mints=len(chunk))
                    continue
                body = await r.json()
        except Exception:
            log.exception("birdeye_multi_price_liq_exception", n_mints=len(chunk))
            continue
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            continue
        for mint, entry in data.items():
            if not isinstance(entry, dict):
                continue
            try:
                price = float(entry.get("value") or 0.0)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            try:
                liq = float(entry["liquidity"]) if entry.get("liquidity") is not None else None
            except (TypeError, ValueError):
                liq = None
            out[mint] = {"price": price, "liquidity": liq}
    return out


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
        # No liquidity data → flat slippage estimate for memecoins, floored
        # at the configured paper-slippage minimum.
        ESTIMATED_SLIPPAGE_BPS = max(settings.copy_min_paper_slippage_bps, 100.0)
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


async def fetch_token_creation(
    session: aiohttp.ClientSession,
    mint: str,
) -> Optional[dict]:
    """Fetch a Solana token's on-chain creation time from Birdeye.

    Returns {"created_unix": int, "tx": Optional[str]} or None on any
    failure. Best-effort — NEVER raises, NEVER blocks the entry fill
    (called post-placement). Used to stamp token age at entry
    (sim_metadata.token_age_at_entry_hours) so we can study rug risk by
    token age — rugs cluster in the first minutes-to-hours of a fresh
    mint (see project_fleet_design_state rug-timing research, 2026-06-10:
    median Solana rug lifespan ~17 min, p75 ~1.4h).

    Endpoint: /defi/token_creation_info. One call per entry (entries are
    infrequent — a few per hour) so CU cost is negligible.
    """
    settings = get_copy_settings()
    if not settings.birdeye_api_key:
        return None
    url = "https://public-api.birdeye.so/defi/token_creation_info"
    headers = {"X-API-KEY": settings.birdeye_api_key, "x-chain": "solana"}
    try:
        async with session.get(url, params={"address": mint}, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return None
            body = await r.json()
    except Exception:
        return None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return None
    unix = data.get("blockUnixTime")
    if unix is None:
        return None
    try:
        return {"created_unix": int(unix), "tx": data.get("txHash")}
    except (TypeError, ValueError):
        return None


async def fetch_token_liquidity(
    session: aiohttp.ClientSession,
    mint: str,
) -> Optional[float]:
    """Fetch a Solana token's current pool liquidity (USD) from Birdeye.

    Returns liquidity in USD, or None on failure. Best-effort — never raises.
    Used at paper-sell time to detect rugs: if liquidity has collapsed
    (LP pulled), the token can't actually be sold, so the paper close must
    book a ~total loss instead of a fictitious exit at the stale last price
    (the 2026-06-14 turtle bug: paper-sold a rugged token at +74% / +$295
    when the real outcome was ~-100%).

    Birdeye /defi/price with include_liquidity=true returns data.liquidity.
    """
    settings = get_copy_settings()
    if not settings.birdeye_api_key:
        return None
    url = "https://public-api.birdeye.so/defi/price"
    headers = {"X-API-KEY": settings.birdeye_api_key, "x-chain": "solana"}
    try:
        async with session.get(url, params={"address": mint, "include_liquidity": "true"},
                                headers=headers,
                                timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return None
            body = await r.json()
    except Exception:
        return None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return None
    liq = data.get("liquidity")
    try:
        return float(liq) if liq is not None else None
    except (TypeError, ValueError):
        return None


async def fetch_dexscreener_pair(
    session: aiohttp.ClientSession,
    mint: str,
) -> Optional[dict]:
    """One-call token snapshot from Dexscreener /tokens/v1/solana/{mint} (the promo
    signal's own source). Returns the highest-liquidity pair's fields parsed into a
    flat dict — liquidity, price, age, marketcap, and the practitioner filter-stack
    inputs (liq/mcap ratio, buy/sell ratio, volume acceleration). Best-effort → None.

    Replaces 3 Birdeye round-trips (liquidity + creation + price) for promobuy, and
    unlocks the market-activity features (Paper 1/MELT + practitioner stack) for free.
    """
    url = f"https://api.dexscreener.com/tokens/v1/solana/{mint}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return None
            pairs = await r.json()
    except Exception:
        return None
    return parse_dexscreener_pairs(pairs)


def parse_dexscreener_pairs(pairs) -> Optional[dict]:
    """Pure parser for a Dexscreener /tokens/v1 or /latest/dex response list — picks
    the highest-liquidity pair and flattens the useful fields. Shared by the async
    fetch above and the sync promo_shadow_collector. No I/O."""
    if not isinstance(pairs, list) or not pairs:
        return None
    # main pool = highest USD liquidity
    p = max(pairs, key=lambda x: ((x.get("liquidity") or {}).get("usd") or 0))
    liq = (p.get("liquidity") or {}).get("usd")
    vol = p.get("volume") or {}
    txns = p.get("txns") or {}
    h1 = txns.get("h1") or {}
    buys_h1, sells_h1 = (h1.get("buys") or 0), (h1.get("sells") or 0)
    mcap = p.get("marketCap") or p.get("fdv")
    created_ms = p.get("pairCreatedAt")
    try:
        price_usd = float(p["priceUsd"]) if p.get("priceUsd") is not None else None
    except (TypeError, ValueError):
        price_usd = None
    vol_h1, vol_h6 = float(vol.get("h1") or 0), float(vol.get("h6") or 0)
    out = {
        "price_usd": price_usd,
        "liquidity_usd": float(liq) if liq is not None else None,
        "market_cap": float(mcap) if mcap else None,
        "created_unix": int(created_ms / 1000) if created_ms else None,
        "age_hours": ((time.time() - created_ms / 1000) / 3600.0) if created_ms else None,
        "vol_h1": vol_h1, "vol_h6": vol_h6, "vol_h24": float(vol.get("h24") or 0),
        "buys_h1": buys_h1, "sells_h1": sells_h1,
        "buy_sell_ratio_h1": (buys_h1 / sells_h1) if sells_h1 else None,
        # accel: last-hour volume vs the average hourly rate over 6h (>1 = accelerating)
        "vol_accel": (vol_h1 / (vol_h6 / 6.0)) if vol_h6 > 0 else None,
        "liq_mcap_ratio": (float(liq) / float(mcap)) if (liq and mcap) else None,
        "dex_id": p.get("dexId"),
    }
    return out


async def fetch_first_buyers(
    session: aiohttp.ClientSession,
    mint: str,
    limit: int = 100,
) -> Optional[dict]:
    """Birdeye `/token/v1/first-buyers` — the token's earliest buyers (the launch
    snipers / coordinated ring) + their aggregate sell behavior. LIVE per-token bundle
    detection for ANY token (vs the static RED-COHORT roster). Returns the buyer wallet
    list + dump rate; the caller computes cohort overlap. Best-effort → None.

    `page_summary` over the first `limit` buyers gives buy_more/hold/sell_partial/sell_all;
    first_buyer_sell_all_frac ≈ 1 = the snipers already dumped (we'd be exit liquidity).
    """
    settings = get_copy_settings()
    if not settings.birdeye_api_key:
        return None
    url = (f"https://public-api.birdeye.so/token/v1/first-buyers"
           f"?token_address={mint}&limit={int(limit)}&offset=0")
    headers = {"X-API-KEY": settings.birdeye_api_key, "x-chain": "solana",
               "accept": "application/json"}
    try:
        async with session.get(url, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return None
            body = await r.json()
    except Exception:
        return None
    data = (body or {}).get("data") or {}
    buyers = data.get("buyers") or []
    ps = data.get("page_summary") or {}
    n = int(ps.get("total_wallets") or len(buyers) or 0)
    wallets = [b.get("wallet_address") for b in buyers if b.get("wallet_address")]
    return {
        "n_first_buyers": n,
        "buyer_wallets": wallets,
        "first_buyer_sell_all_frac": (int(ps.get("sell_all") or 0) / n) if n else None,
        "first_buyer_hold_frac": (int(ps.get("hold") or 0) / n) if n else None,
    }


async def fetch_token_security(
    session: aiohttp.ClientSession,
    mint: str,
) -> Optional[dict]:
    """Fetch a Solana token's creator + holder concentration from Birdeye.

    Returns {"creator": str|None, "top10_holder_pct": float|None,
    "owner_pct": float|None} or None on any failure. Best-effort —
    NEVER raises.

    Two uses (both at entry):
      1. Blocklist check — skip the buy if `creator` is a known serial
         net-loss rug deployer (config.get_blocked_creators).
      2. Concentration instrumentation — stamp top10_holder_pct/owner_pct
         into sim_metadata so we can LATER test whether any entry-time
         concentration threshold separates the rug-pumps we profit on
         (NUT/TRILL) from the dead-on-arrival ones we lose on. We do NOT
         filter on concentration today — the 2026-06-10 audit showed a
         blanket concentration filter is -EV (rugs are the profit center,
         and winners/losers share the same fingerprint at entry). This is
         pure instrumentation pending data. See project_fleet_design_state.

    Endpoint: /defi/token_security. Birdeye returns percentages as
    fractions (0-1); stored raw.
    """
    settings = get_copy_settings()
    if not settings.birdeye_api_key:
        return None
    url = "https://public-api.birdeye.so/defi/token_security"
    headers = {"X-API-KEY": settings.birdeye_api_key, "x-chain": "solana"}
    try:
        async with session.get(url, params={"address": mint}, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return None
            body = await r.json()
    except Exception:
        return None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return None

    def _f(v) -> Optional[float]:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return {
        "creator": data.get("creatorAddress") or data.get("ownerAddress"),
        "top10_holder_pct": _f(data.get("top10HolderPercent")),
        "owner_pct": _f(data.get("creatorPercentage") or data.get("ownerPercentage")),
    }


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
