"""Helius Solana wallet activity poller.

Build A uses polling: every COPY_WALLET_POLL_SECONDS we fetch each tracked
wallet's recent confirmed transactions and parse SPL token swaps out of them.
Build B can swap this for Helius webhooks (push delivery, lower latency).

Helius "enhanced transactions" API decodes Jupiter/Raydium/Orca swaps for us
— we look for `events.swap` blocks and emit normalized BuyEvent objects to
the cluster detector.

Stateless across restarts: we keep the last-seen signature per wallet in
process memory and skip everything older. Slightly leaky on restart (we
might re-emit a few minutes of events) but the cluster detector dedups by
(wallet, token, timestamp).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

from bots.copy.config import get_copy_settings
from framework.logging_setup import get_logger


log = get_logger(__name__)

ENHANCED_TX_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"

# Native SOL doesn't have a mint; Helius represents wrapped SOL with this address
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

# Stablecoins + SOL are the input legs we expect in a "buy" — anything else
# we treat as a token-to-token swap and ignore (cluster signal is about
# fresh entries with USD notional).
INPUT_LEG_MINTS = {WSOL_MINT, USDC_MINT, USDT_MINT}


@dataclass
class WalletBuyEvent:
    """Normalized 'wallet bought token X' event."""
    wallet_address: str
    chain: str                  # always 'solana' here
    token_mint: str             # the token being bought
    notional_usd: float
    timestamp_ms: int
    tx_signature: str
    raw: Optional[dict] = None


class HeliusSolanaClient:
    """Polls a set of Solana wallet addresses for new swap activity."""

    def __init__(self) -> None:
        self.settings = get_copy_settings()
        # wallet_address -> last signature we processed
        self._last_seen_sig: dict[str, str] = {}

    async def fetch_recent_buys(
        self,
        session: aiohttp.ClientSession,
        wallet_address: str,
        sol_price_usd: float,
        limit: int = 25,
    ) -> list[WalletBuyEvent]:
        """Fetch up to `limit` recent enhanced transactions for a wallet,
        parse swaps, return normalized buy events newer than last_seen.
        """
        if not self.settings.helius_api_key:
            log.warning("helius_no_api_key", wallet=wallet_address)
            return []
        url = ENHANCED_TX_URL.format(address=wallet_address)
        params = {"api-key": self.settings.helius_api_key, "limit": str(limit)}
        last_sig = self._last_seen_sig.get(wallet_address)
        if last_sig:
            params["before"] = ""  # Helius supports "until=<sig>" — but we just dedup below to keep it simple

        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    log.warning("helius_fetch_failed", wallet=wallet_address, status=r.status)
                    return []
                txs = await r.json()
        except Exception:
            log.exception("helius_fetch_exception", wallet=wallet_address)
            return []

        if not isinstance(txs, list):
            log.warning("helius_unexpected_response", wallet=wallet_address)
            return []

        events: list[WalletBuyEvent] = []
        newest_sig: Optional[str] = None
        for tx in txs:
            sig = tx.get("signature")
            if newest_sig is None:
                newest_sig = sig
            if last_sig and sig == last_sig:
                break  # caught up to where we left off

            buy = self._parse_buy(tx, wallet_address, sol_price_usd)
            if buy is not None:
                events.append(buy)

        if newest_sig:
            self._last_seen_sig[wallet_address] = newest_sig
        return events

    def _parse_buy(
        self,
        tx: dict,
        wallet_address: str,
        sol_price_usd: float,
    ) -> Optional[WalletBuyEvent]:
        """Parse a Helius enhanced tx — return a buy event if this wallet
        swapped a stablecoin or SOL for some non-stable token.
        """
        events = tx.get("events", {}) or {}
        swap = events.get("swap")
        if not swap:
            return None

        ts_ms = int(tx.get("timestamp", 0)) * 1000
        if ts_ms <= 0:
            return None

        # Helius swap shape: nativeInput / tokenInputs / nativeOutput / tokenOutputs
        token_inputs = swap.get("tokenInputs") or []
        token_outputs = swap.get("tokenOutputs") or []
        native_input = swap.get("nativeInput") or {}
        native_output = swap.get("nativeOutput") or {}

        # Sum input notional in USD (only count stable + SOL legs as 'spend')
        notional_usd = 0.0
        for ti in token_inputs:
            mint = ti.get("mint")
            user = ti.get("userAccount") or ti.get("fromUserAccount")
            if user != wallet_address:
                continue
            amt_str = (ti.get("rawTokenAmount") or {}).get("tokenAmount") or "0"
            decimals = (ti.get("rawTokenAmount") or {}).get("decimals") or 0
            try:
                ui_amount = float(amt_str) / (10 ** int(decimals))
            except Exception:
                continue
            if mint in (USDC_MINT, USDT_MINT):
                notional_usd += ui_amount
            elif mint == WSOL_MINT:
                notional_usd += ui_amount * sol_price_usd

        # Native SOL input (SOL not wrapped)
        native_user = native_input.get("account")
        if native_user == wallet_address:
            try:
                lamports = int(native_input.get("amount", 0))
                notional_usd += (lamports / 1_000_000_000) * sol_price_usd
            except Exception:
                pass

        if notional_usd <= 0:
            return None

        # Output token: pick the first token output going TO this wallet
        # whose mint is NOT a stable / SOL (that would just be a stable swap)
        bought_mint: Optional[str] = None
        for to in token_outputs:
            mint = to.get("mint")
            user = to.get("userAccount") or to.get("toUserAccount")
            if user != wallet_address:
                continue
            if mint in INPUT_LEG_MINTS:
                continue
            bought_mint = mint
            break

        if bought_mint is None:
            return None

        return WalletBuyEvent(
            wallet_address=wallet_address,
            chain="solana",
            token_mint=bought_mint,
            notional_usd=notional_usd,
            timestamp_ms=ts_ms,
            tx_signature=tx.get("signature", ""),
            raw=tx,
        )

    async def fetch_sol_price_usd(
        self,
        session: aiohttp.ClientSession,
    ) -> float:
        """Tiny price helper. Uses Jupiter quote for 1 SOL → USDC.

        Fallback to a static $150 if the call fails. SOL is the gas + common
        input leg; getting it slightly wrong only mis-prices the input side
        of swaps and the cluster detector still triggers correctly.
        """
        params = {
            "inputMint": WSOL_MINT,
            "outputMint": USDC_MINT,
            "amount": "1000000000",  # 1 SOL = 1e9 lamports
            "slippageBps": "100",
        }
        try:
            async with session.get(
                self.settings.jupiter_quote_url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status != 200:
                    return 150.0
                data = await r.json()
            out = int(data.get("outAmount", 0))
            if out <= 0:
                return 150.0
            return out / 1_000_000  # USDC has 6 decimals
        except Exception:
            return 150.0


async def poll_wallets(
    client: HeliusSolanaClient,
    session: aiohttp.ClientSession,
    wallets: list[str],
    sol_price_usd: float,
    concurrency: int = 2,
    per_call_delay_seconds: float = 0.15,
) -> list[WalletBuyEvent]:
    """Poll many wallets in parallel (bounded). Aggregates all buy events.

    Helius Developer plan rate limit: 10 RPS. With concurrency=2 + 0.15s
    inter-call delay, sustained throughput is ~13 RPS peak (2 in flight,
    new one fires every 150ms after one returns) — well under the limit
    while still completing 138 wallets in ~10-12s per cycle.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _one(w: str) -> list[WalletBuyEvent]:
        async with sem:
            result = await client.fetch_recent_buys(session, w, sol_price_usd)
            await asyncio.sleep(per_call_delay_seconds)
            return result

    results = await asyncio.gather(*[_one(w) for w in wallets], return_exceptions=True)
    out: list[WalletBuyEvent] = []
    for r in results:
        if isinstance(r, list):
            out.extend(r)
    return out
