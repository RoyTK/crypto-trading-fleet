"""Cielo API client.

Two responsibilities:
1. Wallet curation — pull top-trader leaderboard + per-wallet PnL summaries
   for `scripts/curate_wallet_pool.py`. Build A blocker.
2. EVM wallet activity feed — Helius covers Solana; Cielo covers Base +
   Arbitrum (and gives us a uniform feed shape across wallets).

Cielo's API requires the X-API-Key header. All endpoints are REST/JSON.
We're intentionally building a thin wrapper — the curation script can grow
its own logic on top of `top_traders()` and `wallet_pnl()`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import aiohttp

from bots.copy.config import (
    WALLET_MIN_AGE_DAYS,
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
    address: str
    chain: str
    pnl_usd_180d: float
    win_rate: float          # 0..1
    avg_hold_minutes: float
    trade_count_90d: int
    wallet_age_days: int
    raw: Optional[dict] = None


class CieloClient:
    """Thin async wrapper around the Cielo Premium API."""

    def __init__(self) -> None:
        self.settings = get_copy_settings()

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.settings.cielo_api_key, "Accept": "application/json"}

    # ---- Curation: leaderboard + per-wallet PnL ---------------------------

    async def top_traders(
        self,
        session: aiohttp.ClientSession,
        chain: str,
        limit: int = 200,
        timeframe: str = "180d",
    ) -> list[str]:
        """Return wallet addresses from Cielo's top-trader leaderboard for a chain."""
        if not self.settings.cielo_api_key:
            log.warning("cielo_no_api_key")
            return []
        url = f"{self.settings.cielo_api_base}/leaderboard/top-traders"
        params = {"chain": chain, "limit": str(limit), "timeframe": timeframe}
        try:
            async with session.get(url, params=params, headers=self._headers(),
                                    timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    log.warning("cielo_top_traders_failed", chain=chain, status=r.status)
                    return []
                data = await r.json()
        except Exception:
            log.exception("cielo_top_traders_exception", chain=chain)
            return []
        addrs = []
        for item in data.get("data", []) or []:
            a = item.get("wallet_address") or item.get("address")
            if a:
                addrs.append(a)
        return addrs

    async def wallet_stats(
        self,
        session: aiohttp.ClientSession,
        address: str,
        chain: str,
    ) -> Optional[WalletStats]:
        """Pull aggregate PnL/winrate/holding stats for one wallet."""
        if not self.settings.cielo_api_key:
            return None
        url = f"{self.settings.cielo_api_base}/wallet/{address}/pnl-summary"
        params = {"chain": chain, "timeframe": "180d"}
        try:
            async with session.get(url, params=params, headers=self._headers(),
                                    timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    log.warning("cielo_wallet_stats_failed", address=address, status=r.status)
                    return None
                data = await r.json()
        except Exception:
            log.exception("cielo_wallet_stats_exception", address=address)
            return None

        d = data.get("data") or {}
        try:
            return WalletStats(
                address=address,
                chain=chain,
                pnl_usd_180d=float(d.get("realized_pnl_usd", 0) or 0),
                win_rate=float(d.get("winrate", 0) or 0),
                avg_hold_minutes=float(d.get("avg_holding_time_minutes", 0) or 0),
                trade_count_90d=int(d.get("trades_count_90d", 0) or 0),
                wallet_age_days=int(d.get("wallet_age_days", 0) or 0),
                raw=d,
            )
        except Exception:
            log.exception("cielo_wallet_stats_parse_failed", address=address)
            return None

    # ---- Activity feed (EVM Base/Arbitrum) ---------------------------------

    async def fetch_recent_buys(
        self,
        session: aiohttp.ClientSession,
        address: str,
        chain: str,
        limit: int = 25,
    ) -> list[WalletBuyEvent]:
        """Return WalletBuyEvent objects for an EVM wallet's recent token buys."""
        if not self.settings.cielo_api_key:
            return []
        if chain not in ("base", "arbitrum"):
            log.warning("cielo_unsupported_chain", chain=chain)
            return []
        url = f"{self.settings.cielo_api_base}/wallet/{address}/feed"
        params = {"chain": chain, "limit": str(limit), "tx_types": "swap"}
        try:
            async with session.get(url, params=params, headers=self._headers(),
                                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    log.warning("cielo_feed_failed", address=address, chain=chain, status=r.status)
                    return []
                data = await r.json()
        except Exception:
            log.exception("cielo_feed_exception", address=address, chain=chain)
            return []

        events: list[WalletBuyEvent] = []
        for item in data.get("data", []) or []:
            ev = self._parse_buy(item, address, chain)
            if ev is not None:
                events.append(ev)
        return events

    def _parse_buy(self, item: dict, wallet: str, chain: str) -> Optional[WalletBuyEvent]:
        if (item.get("tx_type") or "").lower() != "swap":
            return None
        bought_token = (
            item.get("token_bought_address")
            or item.get("token_out_address")
            or item.get("token_to")
        )
        if not bought_token:
            return None
        notional = float(
            item.get("usd_value")
            or item.get("amount_usd")
            or item.get("token_sold_usd")
            or 0
        )
        if notional <= 0:
            return None
        ts = item.get("timestamp") or item.get("block_time") or 0
        try:
            ts_ms = int(ts) * 1000 if int(ts) < 10**12 else int(ts)
        except Exception:
            return None
        return WalletBuyEvent(
            wallet_address=wallet,
            chain=chain,
            token_mint=bought_token,
            notional_usd=notional,
            timestamp_ms=ts_ms,
            tx_signature=item.get("tx_hash", "") or item.get("hash", ""),
            raw=item,
        )


def passes_curation_filters(stats: WalletStats) -> tuple[bool, list[str]]:
    """Apply locked Item #7 wallet curation criteria. Returns (passed, reasons_failed)."""
    reasons: list[str] = []
    if stats.pnl_usd_180d < WALLET_MIN_PNL_USD:
        reasons.append(f"pnl_below_${WALLET_MIN_PNL_USD:.0f}")
    if stats.win_rate < WALLET_MIN_WIN_RATE:
        reasons.append(f"winrate_below_{WALLET_MIN_WIN_RATE:.2f}")
    if stats.trade_count_90d < WALLET_MIN_TRADES_90D:
        reasons.append(f"trades_90d_below_{WALLET_MIN_TRADES_90D}")
    if stats.wallet_age_days < WALLET_MIN_AGE_DAYS:
        reasons.append(f"age_below_{WALLET_MIN_AGE_DAYS}d")
    # Hold time: 30 min ≤ avg ≤ 7 days
    if stats.avg_hold_minutes < 30 or stats.avg_hold_minutes > 7 * 24 * 60:
        reasons.append("hold_time_out_of_band")
    return len(reasons) == 0, reasons
