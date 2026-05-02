"""Hyperliquid SDK wrapper.

Provides:
- Read-only market data (Info) — used in Build A and beyond
- Order placement (Exchange) — Build B only; methods raise if agent key missing

The wrapper isolates SDK quirks from the rest of the bot. If we ever need to
add a fallback venue, it goes here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from bots.structure.config import get_structure_settings
from framework.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class L2Book:
    asset: str
    bids: list[tuple[float, float]]   # [(price, size), ...] descending price
    asks: list[tuple[float, float]]   # [(price, size), ...] ascending price
    raw: Any = None


@dataclass
class AssetCtx:
    asset: str
    mid_price: float
    funding_rate_hourly: float       # decimal, e.g. 0.0001 = 1bp / hour
    open_interest_usd: float
    day_volume_usd: float


@dataclass
class WhalePosition:
    asset: str
    size_native: float                # signed; +long, -short
    notional_usd: float               # signed; +long, -short
    entry_price: Optional[float] = None


@dataclass
class LiquidationEvent:
    asset: str
    side: str                          # 'long' or 'short' (which side got liquidated)
    notional_usd: float
    price: float
    timestamp_ms: int


class HyperliquidVenue:
    """Lazy-init wrapper. Constructing this is cheap; SDK calls happen on use."""

    def __init__(self) -> None:
        self.settings = get_structure_settings()
        self._info = None
        self._exchange = None

    # ---- Read-only (Build A) -----------------------------------------------

    @property
    def info(self):
        if self._info is None:
            from hyperliquid.info import Info
            self._info = Info(self.settings.hyperliquid_api_url, skip_ws=True)
        return self._info

    def all_mids(self) -> dict[str, float]:
        raw = self.info.all_mids()
        return {asset: float(price) for asset, price in raw.items()}

    def sz_decimals(self, asset: str) -> int:
        """Lookup the szDecimals for an asset from cached meta.

        Hyperliquid rejects orders whose size doesn't round cleanly to the
        asset's szDecimals precision. Default fallback to 4 if asset unknown.
        """
        if not hasattr(self, "_sz_decimals_cache") or not self._sz_decimals_cache:
            try:
                meta = self.info.meta()
                self._sz_decimals_cache = {
                    a.get("name"): int(a.get("szDecimals", 4))
                    for a in meta.get("universe", [])
                    if a.get("name")
                }
            except Exception:
                log.exception("sz_decimals_cache_fetch_failed")
                self._sz_decimals_cache = {}
        return self._sz_decimals_cache.get(asset, 4)

    def asset_contexts(self) -> list[AssetCtx]:
        """Return per-asset metadata: mid, funding, OI, day volume.

        Uses metaAndAssetCtxs which returns universe + per-asset ctxs in one call.
        """
        meta_and_ctxs = self.info.meta_and_asset_ctxs()
        try:
            meta, ctxs = meta_and_ctxs
        except Exception:
            log.warning("meta_and_asset_ctxs_unexpected_shape", payload_type=type(meta_and_ctxs).__name__)
            return []
        universe = meta.get("universe", []) if isinstance(meta, dict) else []
        out: list[AssetCtx] = []
        for asset_def, ctx in zip(universe, ctxs):
            try:
                name = asset_def.get("name", "")
                mid = float(ctx.get("midPx") or ctx.get("markPx") or 0.0)
                funding = float(ctx.get("funding") or 0.0)
                oi = float(ctx.get("openInterest") or 0.0) * mid
                day_vol = float(ctx.get("dayNtlVlm") or 0.0)
                out.append(AssetCtx(
                    asset=name,
                    mid_price=mid,
                    funding_rate_hourly=funding,
                    open_interest_usd=oi,
                    day_volume_usd=day_vol,
                ))
            except Exception:
                log.warning("asset_ctx_parse_failed", asset=asset_def)
        return out

    def l2_book(self, asset: str) -> L2Book:
        raw = self.info.l2_snapshot(asset)
        levels = raw.get("levels", [[], []])
        bids_raw, asks_raw = levels[0], levels[1]
        bids = [(float(lv["px"]), float(lv["sz"])) for lv in bids_raw]
        asks = [(float(lv["px"]), float(lv["sz"])) for lv in asks_raw]
        bids.sort(key=lambda x: -x[0])
        asks.sort(key=lambda x: x[0])
        return L2Book(asset=asset, bids=bids, asks=asks, raw=raw)

    def user_positions(self, address: str) -> list[WhalePosition]:
        state = self.info.user_state(address)
        out: list[WhalePosition] = []
        for asset_pos in state.get("assetPositions", []):
            pos = asset_pos.get("position", {})
            asset = pos.get("coin", "")
            szi = pos.get("szi")
            if szi is None:
                continue
            size_native = float(szi)
            if size_native == 0:
                continue
            entry_px = pos.get("entryPx")
            entry_price = float(entry_px) if entry_px else None
            position_value = pos.get("positionValue")
            notional_usd = float(position_value) if position_value else (
                size_native * (entry_price or 0.0)
            )
            # Convention: positive notional for longs, negative for shorts
            if size_native < 0:
                notional_usd = -abs(notional_usd)
            else:
                notional_usd = abs(notional_usd)
            out.append(WhalePosition(
                asset=asset,
                size_native=size_native,
                notional_usd=notional_usd,
                entry_price=entry_price,
            ))
        return out

    # ---- Order placement (Build B) -----------------------------------------

    @property
    def exchange(self):
        if self._exchange is None:
            agent_key = self.settings.hyperliquid_agent_private_key
            master = self.settings.hyperliquid_master_address
            if not agent_key or not master:
                raise RuntimeError(
                    "Exchange unavailable: HYPERLIQUID_AGENT_PRIVATE_KEY or "
                    "HYPERLIQUID_MASTER_ADDRESS not set. Build B requires both."
                )
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            agent = Account.from_key(agent_key)
            self._exchange = Exchange(
                agent,
                self.settings.hyperliquid_api_url,
                account_address=master,
            )
        return self._exchange


def is_exchange_available() -> bool:
    s = get_structure_settings()
    return bool(s.hyperliquid_agent_private_key and s.hyperliquid_master_address)
