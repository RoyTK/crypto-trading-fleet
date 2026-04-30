"""STRUCTURE bot — main entry point.

Build A scope:
- 3 signal generators (Funding Fade, Liquidation Cascade, Whale Flip)
- Median-realistic fill simulator
- Paper-mode trades only (no real orders; no shadow yet)
- Per-bot DD halts via framework/halt_state
- Position management (stops, take-profits, timeouts)
- Reconciliation against Hyperliquid user_state for the master wallet

Build B (later) adds the shadow execution layer in `executor.py` and wires
10% sampling of paper signals into real $5-20 orders.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import time
from typing import Any, Optional

from bots.base.bot_lifecycle import BotLifecycle
from bots.structure.config import (
    LIQ_TOP_VOL_RANK,
    get_structure_settings,
)
from bots.structure.fill_simulator import StructureFillSimulator
from bots.structure.loop_helpers import (
    OpenPaperTrade,
    close_paper_trade,
    has_open_position,
    list_open_paper_trades,
    panic_close_all_open,
    persist_paper_trade,
    persist_signal,
)
from bots.structure.reconciliation import make_fetcher
from bots.structure.signals import funding_fade
from bots.structure.signals.liquidation_cascade import LiquidationCascadeDetector
from bots.structure.signals.whale_flip import WhaleFlipDetector
from bots.structure.signals.base import SignalCandidate
from bots.structure.sizing import size_position
from bots.structure.venue import HyperliquidVenue, L2Book, LiquidationEvent, AssetCtx
from framework.alerts import emit_alert
from framework.reconciliation import register_venue_fetcher
from monitoring.alerting.taxonomy import Severity


WHALE_LIST_PATH = Path(__file__).parent / "whale_list.json"


class StructureBot(BotLifecycle):
    bot_id = "structure"

    def __init__(self) -> None:
        super().__init__()
        self.struct_settings = get_structure_settings()
        self.loop_interval_seconds = self.struct_settings.structure_loop_interval_seconds
        self.venue = HyperliquidVenue()
        self.simulator = StructureFillSimulator()
        self.liq_detector = LiquidationCascadeDetector()
        self.whale_detector = WhaleFlipDetector()
        self._whale_list: list[dict[str, Any]] = []
        self._asset_ctx_cache: list[AssetCtx] = []
        self._last_funding_poll_ts: float = 0.0
        self._last_whale_poll_ts: float = 0.0
        self._liq_ws_task: Optional[asyncio.Task] = None

    # ---- Lifecycle hooks ---------------------------------------------------

    async def on_start(self) -> None:
        register_venue_fetcher("hyperliquid", make_fetcher(self.venue))
        self._whale_list = self._load_whale_list()
        self.log.info(
            "structure_started",
            paper_capital_usd=self.struct_settings.structure_paper_capital_usd,
            whale_count=len(self._whale_list),
        )
        # Pre-seed the asset context cache so first iterate() has data
        await self._refresh_asset_contexts()
        self._liq_ws_task = asyncio.create_task(self._liquidation_ws_loop())

    async def on_stop(self) -> None:
        if self._liq_ws_task:
            self._liq_ws_task.cancel()

    async def on_panic(self, payload: dict[str, Any]) -> None:
        n = panic_close_all_open()
        self.log.warning("panic_closed_paper_trades", count=n, actor=payload.get("actor"))

    # ---- Main iterate ------------------------------------------------------

    async def iterate(self) -> None:
        now = time()

        # Funding rate poll (also refreshes asset_ctxs which the liq detector uses)
        if now - self._last_funding_poll_ts >= self.struct_settings.structure_funding_poll_seconds:
            await self._refresh_asset_contexts()
            self._evaluate_funding_fade()
            self._evaluate_liquidation_cascade()
            self._last_funding_poll_ts = now

        # Whale poll
        if now - self._last_whale_poll_ts >= self.struct_settings.structure_whale_poll_seconds:
            await self._poll_whales()
            self._evaluate_whale_flips()
            self._last_whale_poll_ts = now

        # Position management every iteration
        await self._manage_open_positions()

    # ---- Periodic actions --------------------------------------------------

    async def _refresh_asset_contexts(self) -> None:
        try:
            ctxs = await asyncio.to_thread(self.venue.asset_contexts)
            if ctxs:
                self._asset_ctx_cache = ctxs
        except Exception:
            self.log.exception("asset_contexts_refresh_failed")

    def _evaluate_funding_fade(self) -> None:
        if not self._asset_ctx_cache:
            return
        candidates = funding_fade.evaluate(self._asset_ctx_cache)
        for c in candidates:
            self._consume_candidate(c)

    def _evaluate_liquidation_cascade(self) -> None:
        if not self._asset_ctx_cache:
            return
        # Feed prices into the detector for the price-move check
        now_ms = int(time() * 1000)
        for ctx in self._asset_ctx_cache[:LIQ_TOP_VOL_RANK]:
            if ctx.mid_price > 0:
                self.liq_detector.observe_price(ctx.asset, ctx.mid_price, now_ms)
        candidates = self.liq_detector.evaluate(self._asset_ctx_cache, now_ms)
        for c in candidates:
            self._consume_candidate(c)

    async def _poll_whales(self) -> None:
        if not self._whale_list:
            return
        now_ms = int(time() * 1000)
        for whale in self._whale_list:
            address = whale.get("address")
            if not address:
                continue
            try:
                positions = await asyncio.to_thread(self.venue.user_positions, address)
            except Exception:
                self.log.warning("whale_poll_failed", address=address)
                continue
            self.whale_detector.observe_positions(
                whale_address=address,
                positions=positions,
                now_ts_ms=now_ms,
                whale_metadata={
                    "tag": whale.get("tag"),
                    "historical_win_rate": whale.get("historical_win_rate"),
                },
            )

    def _evaluate_whale_flips(self) -> None:
        for c in self.whale_detector.evaluate():
            self._consume_candidate(c)

    # ---- Signal handling ---------------------------------------------------

    def _consume_candidate(self, candidate: SignalCandidate) -> None:
        # Dedupe: don't open a second position from the same signal type for
        # the same (venue, asset) while one is still open.
        if has_open_position(candidate.asset, candidate.venue, candidate.signal_type):
            return

        try:
            book = self.venue.l2_book(candidate.asset)
        except Exception:
            self.log.warning("l2_book_fetch_failed", asset=candidate.asset)
            return

        notional_usd, leverage = size_position(
            candidate.signal_type,
            self.struct_settings.structure_paper_capital_usd,
            current_dd_today_pct=0.0,  # bot is blind to scoring; DD enforcement is separate
            conviction=candidate.conviction,
        )

        sim_fill = self.simulator.simulate_entry(
            asset=candidate.asset,
            notional_usd=notional_usd,
            leverage=leverage,
            direction=candidate.direction,
            market_snapshot=book,
        )

        signal_id = persist_signal(candidate)
        persist_paper_trade(
            signal_id=signal_id,
            candidate=candidate,
            sim_fill=sim_fill,
            notional_usd=notional_usd,
            leverage=leverage,
        )

    # ---- Position management ----------------------------------------------

    async def _manage_open_positions(self) -> None:
        opens = list_open_paper_trades()
        if not opens:
            return
        try:
            mids = await asyncio.to_thread(self.venue.all_mids)
        except Exception:
            self.log.warning("mids_fetch_failed")
            return

        for trade in opens:
            mid = mids.get(trade.asset)
            if mid is None or mid <= 0:
                continue
            # Direction-aware percent move from entry
            pct_move = (mid - trade.entry_price) / trade.entry_price * 100.0
            if trade.direction == "short":
                pct_move = -pct_move
            # Apply leverage to compute "effective" price move on equity
            equity_pct = pct_move * trade.leverage

            exit_reason: Optional[str] = None
            if trade.stop_pct is not None and equity_pct <= -trade.stop_pct:
                exit_reason = "stop"
            elif trade.take_profit_pct is not None and equity_pct >= trade.take_profit_pct:
                exit_reason = "tp"
            elif trade.timeout_hours is not None:
                age = datetime.now(timezone.utc) - trade.entry_at
                if age >= timedelta(hours=trade.timeout_hours):
                    exit_reason = "timeout"

            if exit_reason is None:
                continue

            try:
                book = self.venue.l2_book(trade.asset)
            except Exception:
                self.log.warning("exit_l2_book_failed", asset=trade.asset)
                continue
            exit_fill = self.simulator.simulate_exit(
                asset=trade.asset,
                entry_price=trade.entry_price,
                exit_target_price=mid,
                notional_usd=trade.size_usd,
                leverage=trade.leverage,
                direction=trade.direction,
                market_snapshot=book,
            )
            if exit_fill.fill_price is None:
                # Couldn't simulate an exit — log P2 but don't crash. Try again next tick.
                emit_alert(
                    severity=Severity.P2,
                    title="Could not simulate exit",
                    body=f"asset={trade.asset} reason={exit_fill.no_fill_reason}",
                    bot_id=self.bot_id,
                    event_type="exit_no_fill",
                )
                continue

            close_paper_trade(
                trade_id=trade.trade_id,
                exit_price=exit_fill.fill_price,
                exit_fill=exit_fill,
                exit_reason=exit_reason,
            )

    # ---- Liquidation websocket --------------------------------------------

    async def _liquidation_ws_loop(self) -> None:
        """Subscribe to Hyperliquid liquidations websocket.

        The hyperliquid-python-sdk's WebsocketManager runs synchronously with a
        callback. We bridge into asyncio by enqueueing events into the
        detector. Reconnect with exponential backoff on disconnect.
        """
        backoff = 1
        while True:
            try:
                from hyperliquid.info import Info
                # Note: separate Info() with skip_ws=False just for the WS
                info_ws = Info(self.struct_settings.hyperliquid_api_url, skip_ws=False)

                def on_liquidation(msg: dict[str, Any]) -> None:
                    try:
                        data = msg.get("data") or msg
                        # Hyperliquid liquidations payload shape (approximate):
                        # {coin, side, sz, price, time, ...}
                        ev = LiquidationEvent(
                            asset=str(data.get("coin", "")),
                            side="long" if data.get("side") in ("B", "buy", "long") else "short",
                            notional_usd=float(data.get("sz", 0)) * float(data.get("price", 0) or 0),
                            price=float(data.get("price", 0) or 0),
                            timestamp_ms=int(data.get("time", time() * 1000)),
                        )
                        if ev.asset and ev.notional_usd > 0:
                            self.liq_detector.observe_liquidation(ev)
                    except Exception:
                        self.log.exception("liquidation_event_parse_failed")

                info_ws.subscribe({"type": "liquidations"}, on_liquidation)
                self.log.info("liq_ws_subscribed")
                backoff = 1
                # Keep the task alive until cancelled
                while True:
                    await asyncio.sleep(60)
            except asyncio.CancelledError:
                return
            except Exception:
                self.log.exception("liq_ws_failed", backoff_seconds=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    # ---- Whale list --------------------------------------------------------

    def _load_whale_list(self) -> list[dict[str, Any]]:
        if not WHALE_LIST_PATH.exists():
            self.log.warning("whale_list_missing", path=str(WHALE_LIST_PATH))
            return []
        try:
            with open(WHALE_LIST_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            whales = data.get("whales", [])
            return [w for w in whales if w.get("address")]
        except Exception:
            self.log.exception("whale_list_load_failed")
            return []


def main() -> None:
    bot = StructureBot()
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
