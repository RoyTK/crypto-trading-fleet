"""Birdeye API client — wallet discovery for COPY bot curation.

Confirmed against docs.birdeye.so (2026-05-03):
- Base URL: https://public-api.birdeye.so
- Auth: X-API-KEY header + x-chain: solana header
- /trader/gainers-losers: top traders by PnL globally per chain
  Free Standard tier: includes this endpoint, 30k CU/mo, 1 RPS
- /defi/v2/tokens/top_traders: top traders for a specific token
  Free Standard tier: also included
- limit=10 max per call; paginate with offset up to 10000

We use this for QUARTERLY wallet pool curation, not runtime polling. Free
tier is plenty for a quarterly batch (a few hundred CUs per refresh).
Upgrade to Lite ($39/mo) only if we move to weekly/daily refreshes later.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import aiohttp

from bots.copy.config import get_copy_settings
from framework.logging_setup import get_logger


log = get_logger(__name__)

BASE_URL = "https://public-api.birdeye.so"
LIMIT_PER_CALL = 10  # API caps each call at 10 results


@dataclass
class BirdeyeTopTrader:
    """One entry from the global top-trader leaderboard."""
    address: str
    chain: str
    pnl_usd: Optional[float] = None
    volume_usd: Optional[float] = None
    trade_count: Optional[int] = None
    raw: Optional[dict] = None


class BirdeyeClient:
    """Async wrapper around Birdeye's public API."""

    def __init__(self) -> None:
        self.settings = get_copy_settings()

    def _headers(self, chain: str = "solana") -> dict[str, str]:
        return {
            "X-API-KEY": self.settings.birdeye_api_key,
            "x-chain": chain,
            "Accept": "application/json",
        }

    async def gainers_losers(
        self,
        session: aiohttp.ClientSession,
        chain: str = "solana",
        timeframe: str = "1W",
        sort_by: str = "PnL",
        sort_type: str = "desc",
        target_count: int = 100,
        rate_limit_seconds: float = 1.1,
    ) -> list[BirdeyeTopTrader]:
        """Fetch up to `target_count` top traders. Paginates via offset.

        timeframe ∈ {'yesterday', 'today', '1W'}.
        Free tier RPS=1 → we sleep 1.1s between calls.
        """
        if not self.settings.birdeye_api_key:
            log.warning("birdeye_no_api_key")
            return []

        url = f"{BASE_URL}/trader/gainers-losers"
        out: list[BirdeyeTopTrader] = []
        offset = 0
        while len(out) < target_count and offset < 10_000:
            page_limit = min(LIMIT_PER_CALL, target_count - len(out))
            params = {
                "type": timeframe,
                "sort_by": sort_by,
                "sort_type": sort_type,
                "offset": str(offset),
                "limit": str(page_limit),
            }
            try:
                async with session.get(
                    url, params=params, headers=self._headers(chain),
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as r:
                    if r.status == 429:
                        log.warning("birdeye_rate_limited_backoff", offset=offset)
                        await asyncio.sleep(5)
                        continue
                    if r.status != 200:
                        log.warning("birdeye_gainers_failed", status=r.status, offset=offset)
                        break
                    data = await r.json()
            except Exception:
                log.exception("birdeye_gainers_exception", offset=offset)
                break

            items = ((data.get("data") or {}).get("items")
                     or data.get("data")
                     or [])
            if not items:
                break  # no more data

            for item in items:
                t = self._parse_trader(item, chain)
                if t is not None:
                    out.append(t)

            if len(items) < page_limit:
                break  # last page
            offset += page_limit
            await asyncio.sleep(rate_limit_seconds)

        return out

    async def token_top_traders(
        self,
        session: aiohttp.ClientSession,
        token_address: str,
        chain: str = "solana",
        time_frame: str = "24h",
        sort_by: str = "total_pnl",
        target_count: int = 50,
        rate_limit_seconds: float = 1.1,
    ) -> list[BirdeyeTopTrader]:
        """Fetch top traders for a specific token. Solana memecoin alpha."""
        if not self.settings.birdeye_api_key:
            return []

        url = f"{BASE_URL}/defi/v2/tokens/top_traders"
        out: list[BirdeyeTopTrader] = []
        offset = 0
        while len(out) < target_count and offset < 10_000:
            page_limit = min(LIMIT_PER_CALL, target_count - len(out))
            params = {
                "address": token_address,
                "time_frame": time_frame,
                "sort_by": sort_by,
                "sort_type": "desc",
                "offset": str(offset),
                "limit": str(page_limit),
            }
            try:
                async with session.get(
                    url, params=params, headers=self._headers(chain),
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as r:
                    if r.status == 429:
                        await asyncio.sleep(5)
                        continue
                    if r.status != 200:
                        log.warning("birdeye_token_traders_failed",
                                    status=r.status, token=token_address)
                        break
                    data = await r.json()
            except Exception:
                log.exception("birdeye_token_traders_exception", token=token_address)
                break

            items = ((data.get("data") or {}).get("items")
                     or data.get("data")
                     or [])
            if not items:
                break

            for item in items:
                t = self._parse_trader(item, chain)
                if t is not None:
                    out.append(t)

            if len(items) < page_limit:
                break
            offset += page_limit
            await asyncio.sleep(rate_limit_seconds)

        return out

    def _parse_trader(self, item: dict, chain: str) -> Optional[BirdeyeTopTrader]:
        addr = (
            item.get("address")
            or item.get("owner")
            or item.get("wallet")
            or item.get("walletAddress")
        )
        if not addr:
            return None
        # Field naming varies by endpoint; try common aliases
        pnl = item.get("pnl") or item.get("totalPnl") or item.get("total_pnl")
        vol = item.get("volume") or item.get("volumeUsd") or item.get("volume_usd")
        trades = item.get("trade") or item.get("trades") or item.get("trade_count")
        return BirdeyeTopTrader(
            address=str(addr),
            chain=chain,
            pnl_usd=float(pnl) if pnl is not None else None,
            volume_usd=float(vol) if vol is not None else None,
            trade_count=int(trades) if trades is not None else None,
            raw=item,
        )
