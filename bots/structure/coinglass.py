"""Coinglass v4 API client — Hyperliquid liquidation aggregates.

Hyperliquid does NOT expose a public liquidations feed (subscription type
'liquidations' returns 0 events; trades WS has no liquidation flag). We
source liquidation cascade data from Coinglass instead, which aggregates
liquidations across major perp venues including HL.

Cheapest tier: Hobbyist $29/mo, 30 req/min, 80+ endpoints.
At 30s polling × top-15 assets = 30 calls/min — fits exactly.

Endpoint used:
  GET /api/futures/liquidation/history
  Query: exchange=Hyperliquid, symbol=BTCUSDT, interval=5m, limit=12
  Returns aggregate long_liquidation_usd + short_liquidation_usd per
  interval bucket. We poll for the latest 1h (12 × 5m buckets) every 30s
  and feed the most-recent bucket(s) into the detector.

API key: COINGLASS_API_KEY env var (header: CG-API-KEY).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

import requests

from framework.logging_setup import get_logger


log = get_logger(__name__)

BASE_URL = "https://open-api-v4.coinglass.com"
LIQ_HISTORY_PATH = "/api/futures/liquidation/history"


@dataclass
class LiquidationAggregate:
    """One time-bucket of aggregated liquidations on a single asset."""
    asset: str             # bare symbol, e.g. "BTC"
    bucket_start_ms: int
    long_liq_usd: float    # USD value of LONGS liquidated in this bucket
    short_liq_usd: float   # USD value of SHORTS liquidated
    interval: str          # e.g. "5m"


class CoinglassClient:
    """Lightweight REST client for the Coinglass v4 liquidation endpoint."""

    def __init__(self, api_key: Optional[str] = None, timeout: float = 10.0) -> None:
        self.api_key = api_key or os.environ.get("COINGLASS_API_KEY", "")
        self.timeout = timeout
        self._session: Optional[requests.Session] = None

    def _session_or_fail(self) -> requests.Session:
        if not self.api_key:
            raise RuntimeError(
                "COINGLASS_API_KEY not set — required for liquidation data. "
                "Sign up at https://www.coinglass.com (Hobbyist $29/mo)."
            )
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "CG-API-KEY": self.api_key,
                "Accept": "application/json",
            })
        return self._session

    def liquidation_history(
        self,
        asset: str,
        *,
        interval: str = "5m",
        limit: int = 12,
        exchange: str = "Hyperliquid",
    ) -> list[LiquidationAggregate]:
        """Fetch the most-recent `limit` interval buckets for an asset.

        Returns time-ordered list of LiquidationAggregate (oldest first).
        Empty list on auth/network failure (logs the error).
        """
        session = self._session_or_fail()
        # HL symbol convention on Coinglass is typically <ASSET>USDT for perps.
        symbol = f"{asset}USDT"
        params = {
            "exchange": exchange,
            "symbol": symbol,
            "interval": interval,
            "limit": str(limit),
        }
        try:
            r = session.get(BASE_URL + LIQ_HISTORY_PATH, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            log.warning("coinglass_request_failed", asset=asset, error=str(e))
            return []
        if r.status_code != 200:
            log.warning("coinglass_non_200", asset=asset, status=r.status_code,
                        body=r.text[:200])
            return []
        try:
            payload = r.json()
        except Exception:
            log.warning("coinglass_json_parse_failed", asset=asset, body=r.text[:200])
            return []

        return _parse_liquidation_history(asset, interval, payload)


def _parse_liquidation_history(
    asset: str,
    interval: str,
    payload: dict[str, Any],
) -> list[LiquidationAggregate]:
    """Tolerant parser for Coinglass v4 liquidation_history response.

    Their schema docs are sparse; we accept either {data: [...]} or a bare
    list. Each row may use `time`/`t` for the bucket start and
    `longLiquidationUsd`/`long_liquidation_usd` (and short variants) for
    notional. Skip rows where either field is missing/non-numeric.
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict):
        # Some Coinglass endpoints wrap data in {list: [...]}
        data = data.get("list") or data.get("rows") or data.get("data")
    if not isinstance(data, list):
        return []

    out: list[LiquidationAggregate] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        ts = row.get("time") or row.get("t") or row.get("createTime")
        long_usd = (
            row.get("longLiquidationUsd")
            or row.get("long_liquidation_usd")
            or row.get("longLiquidation")
        )
        short_usd = (
            row.get("shortLiquidationUsd")
            or row.get("short_liquidation_usd")
            or row.get("shortLiquidation")
        )
        try:
            bucket_start_ms = int(ts)
            l_usd = float(long_usd or 0)
            s_usd = float(short_usd or 0)
        except (TypeError, ValueError):
            continue
        out.append(LiquidationAggregate(
            asset=asset,
            bucket_start_ms=bucket_start_ms,
            long_liq_usd=l_usd,
            short_liq_usd=s_usd,
            interval=interval,
        ))
    out.sort(key=lambda a: a.bucket_start_ms)
    return out
