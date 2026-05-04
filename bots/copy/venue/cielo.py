"""Cielo API client.

Confirmed against developer.cielo.finance/reference (2026-05-03):
- Base URL: https://feed-api.cielo.finance/api/v1
- Auth: X-API-KEY header (case-insensitive)
- /feed endpoint: GET /feed?wallet=X&chains=Y&txTypes=swap&limit=N
  Cost: 3 credits per call when wallet param is set; 5 credits otherwise.
- /trading-stats endpoint: GET /{wallet}/trading-stats?days={1d|7d|30d|max}
  Cost: 30 credits. Returns pnl, winrate, swaps_count, holding distribution, etc.
  Does NOT include wallet age. No 180d window — longest is 'max'.
- NO leaderboard / top-traders endpoint exists. Wallet discovery must use
  hand-seeded inputs (Birdeye, DeBank, X/Twitter, etc.); Cielo is validate-only.

Pro plan ($65/mo): 50k credits/mo, 25 credits/sec, all endpoints.
Polling cost concern: at 3 credits/call × 90 EVM wallets × every 2 min = ~3.5M
credits/mo — far over budget. Build A is Solana-only via Helius; Cielo is used
for one-time curation validation. EVM/Cielo polling joins in Build B (likely
via webhooks, not polling).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import aiohttp

from bots.copy.config import (
    WALLET_MAX_HOLD_DAYS,
    WALLET_MIN_HOLD_MINUTES,
    WALLET_MIN_PNL_USD,
    WALLET_MIN_TRADES_90D,
    WALLET_MIN_WIN_RATE,
    get_copy_settings,
)
from bots.copy.venue.helius_solana import WalletBuyEvent
from framework.logging_setup import get_logger


log = get_logger(__name__)


@dataclass
class WalletStats:
    """Subset of Cielo's /trading-stats response we use for curation filters."""
    address: str
    chain: str
    pnl_usd: float
    win_rate: float                  # 0..1
    avg_hold_minutes: float
    swap_count: int
    consecutive_trading_days: int
    timeframe: str                   # '1d' | '7d' | '30d' | 'max'
    raw: Optional[dict] = None


class CieloClient:
    """Thin async wrapper around the Cielo API (Pro plan)."""

    def __init__(self) -> None:
        self.settings = get_copy_settings()

    def _headers(self) -> dict[str, str]:
        return {"X-API-KEY": self.settings.cielo_api_key, "Accept": "application/json"}

    # ---- Curation: per-wallet trading stats -------------------------------

    async def wallet_trading_stats(
        self,
        session: aiohttp.ClientSession,
        address: str,
        chain: str,
        days: str = "max",
        max_retries: int = 3,
    ) -> Optional[WalletStats]:
        """Fetch /trading-stats for one wallet. 30 credits per call.

        days ∈ {'1d', '7d', '30d', 'max'}. 'max' is longest available.

        On 429 / 403 (rate-limit) and 202 (still computing), backs off and
        retries up to max_retries times. On 400 (no data for wallet) returns
        None immediately — Cielo doesn't track this wallet.
        """
        if not self.settings.cielo_api_key:
            log.warning("cielo_no_api_key")
            return None
        url = f"{self.settings.cielo_api_base}/{address}/trading-stats"
        params = {"days": days}
        attempt = 0
        backoff = 2.0
        while True:
            attempt += 1
            try:
                async with session.get(
                    url, params=params, headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        break
                    if r.status in (400, 403, 404):
                        # Wallet not in Cielo's index — terminal, no retry.
                        # 400/403/404 all mean "no data for this wallet"; the
                        # difference depends on Cielo's internal classification
                        # (anonymous, MM-bot-filtered, recently-created, etc.)
                        log.info("cielo_wallet_not_tracked", address=address, status=r.status)
                        return None
                    if r.status in (202, 429, 502, 503, 504) and attempt <= max_retries:
                        log.info("cielo_retry_after_backoff",
                                 address=address, status=r.status, attempt=attempt, backoff=backoff)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30.0)
                        continue
                    log.warning("cielo_trading_stats_failed", address=address, status=r.status)
                    return None
            except Exception:
                if attempt <= max_retries:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
                log.exception("cielo_trading_stats_exception", address=address)
                return None

        d = data.get("data") or {}
        try:
            avg_hold_sec = float(d.get("average_holding_time_sec", 0) or 0)
            return WalletStats(
                address=address,
                chain=chain,
                pnl_usd=float(d.get("pnl", 0) or 0),
                win_rate=float(d.get("winrate", 0) or 0),
                avg_hold_minutes=avg_hold_sec / 60.0,
                swap_count=int(d.get("swaps_count", 0) or 0),
                consecutive_trading_days=int(d.get("consecutive_trading_days", 0) or 0),
                timeframe=days,
                raw=d,
            )
        except Exception:
            log.exception("cielo_trading_stats_parse_failed", address=address)
            return None

    # ---- Activity feed (single endpoint, multi-chain) ---------------------

    async def fetch_recent_buys(
        self,
        session: aiohttp.ClientSession,
        address: str,
        chain: str,
        limit: int = 25,
    ) -> list[WalletBuyEvent]:
        """Return WalletBuyEvent objects for a wallet's recent token buys.

        3 credits per call (when wallet param is set). Single endpoint covers
        all chains via the chains query param — pass 'base' or 'arbitrum'.
        """
        if not self.settings.cielo_api_key:
            return []
        if chain not in ("base", "arbitrum", "ethereum", "polygon"):
            log.warning("cielo_unsupported_chain", chain=chain)
            return []
        url = f"{self.settings.cielo_api_base}/feed"
        params = {
            "wallet": address,
            "chains": chain,
            "txTypes": "swap",
            "limit": str(limit),
        }
        try:
            async with session.get(
                url, params=params, headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    log.warning("cielo_feed_failed", address=address, chain=chain, status=r.status)
                    return []
                data = await r.json()
        except Exception:
            log.exception("cielo_feed_exception", address=address, chain=chain)
            return []

        # Cielo /feed shape: {"status":"ok","data":{"items":[...], "paging":{...}}}
        items = (data.get("data") or {}).get("items") or []
        events: list[WalletBuyEvent] = []
        for item in items:
            ev = self._parse_buy(item, address, chain)
            if ev is not None:
                events.append(ev)
        return events

    def _parse_buy(self, item: dict, wallet: str, chain: str) -> Optional[WalletBuyEvent]:
        if (item.get("tx_type") or "").lower() != "swap":
            return None
        # /feed swap items expose token bought + token sold + USD amount
        bought_token = (
            item.get("token1_address")
            or item.get("token_bought_address")
            or item.get("token_out_address")
            or item.get("contract_address")
        )
        if not bought_token:
            return None
        notional = float(
            item.get("amount_usd")
            or item.get("usd_value")
            or item.get("token0_amount_usd")
            or 0
        )
        if notional <= 0:
            return None
        ts = item.get("timestamp") or item.get("block_time") or 0
        try:
            ts_int = int(ts)
            ts_ms = ts_int * 1000 if ts_int < 10**12 else ts_int
        except Exception:
            return None
        return WalletBuyEvent(
            wallet_address=wallet,
            chain=chain,
            token_mint=bought_token,
            notional_usd=notional,
            timestamp_ms=ts_ms,
            tx_signature=item.get("tx_hash") or item.get("hash") or "",
            raw=item,
        )


def passes_curation_filters(stats: WalletStats) -> tuple[bool, list[str]]:
    """Apply Item #7 wallet curation criteria — tuned 2026-05-04 for Solana
    memecoin reality (rotates in minutes, not hours like HL futures).

    Filters: pnl ≥ $50k, winrate ≥ 0.55, swap_count ≥ 20, 1 min ≤ avg hold ≤ 7d.
    Dropped: consecutive_trading_days proxy (poor maturity signal on Solana).

    Returns (passed, reasons_failed).
    """
    reasons: list[str] = []
    if stats.pnl_usd < WALLET_MIN_PNL_USD:
        reasons.append(f"pnl_below_${WALLET_MIN_PNL_USD:.0f}")
    if stats.win_rate < WALLET_MIN_WIN_RATE:
        reasons.append(f"winrate_below_{WALLET_MIN_WIN_RATE:.2f}")
    if stats.swap_count < WALLET_MIN_TRADES_90D:
        reasons.append(f"swap_count_below_{WALLET_MIN_TRADES_90D}")
    # Hold time: 1 min ≤ avg ≤ 7 days. Lower bound only excludes true HFT/MM
    # bots; upper bound excludes pure long-term holders.
    if stats.avg_hold_minutes < WALLET_MIN_HOLD_MINUTES:
        reasons.append("hold_time_too_short_hft_bot")
    if stats.avg_hold_minutes > WALLET_MAX_HOLD_DAYS * 24 * 60:
        reasons.append("hold_time_too_long_holder")
    return len(reasons) == 0, reasons
