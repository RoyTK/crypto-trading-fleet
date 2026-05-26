"""COPY bot — main entry point.

Phase 2 Build A scope (paper-only, no DEX execution):
- Solana wallet activity arrives via Helius webhooks → webhook_receiver
  service → Redis pubsub on `copy:buys`. Bot subscribes here, no polling.
- Feed buy events into the cluster detector
- On cluster trigger: signal → DEX-quoted sim fill → paper Trade
- Manage open paper positions (software stops, TP, timeout)
- Reconcile every reconciliation_interval seconds (no-op until Build B)
- Per-bot DD halts via framework.dd_monitor cron (already wired)
- /panic listener via base class

Build B will add bots/copy/executor.py and ~10% shadow execution.

Polling-based ingestion (helius_solana.py + cielo.py) is no longer used at
runtime — webhooks replaced it after Helius polling proved too expensive
(10M credits/day at 138 wallets × 10s polling). Cielo client is retained
for one-off curation use; helius_solana for the SOL-price quote helper.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import time
from typing import Any, Optional

import aiohttp
import redis.asyncio as redis_async

from bots.base.bot_lifecycle import BotLifecycle
from bots.base.fill_simulator_base import SimulatedFill
from bots.copy.config import get_copy_settings
from bots.copy.fill_simulator import DEX_FEE_PCT, CopyFillSimulator, CopyMarketSnapshot
from bots.copy.loop_helpers import (
    close_paper_trade,
    has_open_position,
    list_open_paper_trades,
    open_allocation_pct,
    panic_close_all_open,
    persist_paper_trade,
    persist_signal,
)
from bots.copy.reconciliation import make_fetcher_evm, make_fetcher_solana
from bots.copy.signals.base import SignalCandidate
from bots.copy.signals.cluster import ClusterDetector
from bots.copy.sizing import size_position
from bots.copy.venue.dex_quoter import multi_price_solana, quote
from bots.copy.venue.helius_solana import WalletBuyEvent
from framework.config import get_settings as get_framework_settings
from framework.reconciliation import reconcile_once, register_venue_fetcher


REDIS_BUYS_CHANNEL = "copy:buys"
REDIS_MACRO_CLUSTER_CHANNEL = "copy:macro_cluster"

# Solana mint addresses → HL ticker for macro perp assets only.
# When a cluster signal fires on one of these mints, COPY publishes to
# copy:macro_cluster for STRUCTURE's passive subscriber. Engineer's audit
# 2026-05-25 — verify these mints actually appear in production webhook
# stream within 8 hours of deploy; if no cross_bot_signal_log rows
# appear by then, the dict needs updating with whatever mint Helius
# actually emits for the underlying swap.
MACRO_MINT_TO_HL_ASSET: dict[str, str] = {
    "So11111111111111111111111111111111111111112": "SOL",   # native SOL (wrapped form)
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh": "BTC",  # Wormhole WBTC
    "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E": "BTC",  # Allbridge WBTC
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs": "ETH",  # Wormhole ETH
    "2FPyTwcZLUgr5Th81UT8LsjFoGBTHLSYc6M2zHjFMfN": "ETH",   # Allbridge ETH
}


WALLET_POOL_PATH = Path(__file__).parent / "wallet_pool.json"


class CopyBot(BotLifecycle):
    bot_id = "copy"

    def __init__(self) -> None:
        super().__init__()
        self.copy_settings = get_copy_settings()
        self.loop_interval_seconds = self.copy_settings.copy_loop_interval_seconds
        self.simulator = CopyFillSimulator()
        self.cluster = ClusterDetector()
        self._wallets_solana: list[str] = []
        self._wallets_base: list[str] = []
        self._wallets_arbitrum: list[str] = []
        self._last_reconcile_ts: float = 0.0
        self._last_position_check_ts: float = 0.0
        self._session: Optional[aiohttp.ClientSession] = None
        self._buys_subscriber_task: Optional[asyncio.Task] = None
        self._buys_redis: Optional[redis_async.Redis] = None

    # ---- Lifecycle hooks ---------------------------------------------------

    async def on_start(self) -> None:
        register_venue_fetcher("solana", make_fetcher_solana())
        register_venue_fetcher("base", make_fetcher_evm("base"))
        register_venue_fetcher("arbitrum", make_fetcher_evm("arbitrum"))
        self._load_wallet_pool()
        self._session = aiohttp.ClientSession()
        # Subscribe to wallet-buy events on Redis (published by webhook_receiver)
        self._buys_redis = redis_async.from_url(self.settings.redis_url, decode_responses=True)
        self._buys_subscriber_task = asyncio.create_task(self._buys_subscriber())
        self.log.info(
            "copy_started",
            paper_capital_usd=self.copy_settings.copy_paper_capital_usd,
            sol_wallets=len(self._wallets_solana),
            base_wallets=len(self._wallets_base),
            arbitrum_wallets=len(self._wallets_arbitrum),
        )

    async def on_stop(self) -> None:
        if self._buys_subscriber_task:
            self._buys_subscriber_task.cancel()
        if self._buys_redis:
            await self._buys_redis.aclose()
        if self._session:
            await self._session.close()

    async def on_panic(self, payload: dict[str, Any]) -> None:
        # Build A: paper-only. Just mark paper trades closed.
        # Build B will additionally close any open shadow DEX positions.
        n = panic_close_all_open()
        self.log.warning("panic_closed", paper_count=n, actor=payload.get("actor"))

    # ---- Main iterate ------------------------------------------------------

    async def iterate(self) -> None:
        now = time()

        # Cluster evaluation runs every iteration — the Redis subscriber
        # populates cluster state asynchronously, so we just need to check
        # whether any (chain, token) bucket now has 3+ qualifying wallets.
        self._evaluate_clusters()

        # Reconciliation cron
        recon_interval = get_framework_settings().reconciliation_interval_seconds
        if now - self._last_reconcile_ts >= recon_interval:
            try:
                await asyncio.to_thread(reconcile_once)
            except Exception:
                self.log.exception("reconcile_once_failed")
            self._last_reconcile_ts = now

        # Position management on its own cadence (Birdeye free-tier is 1 RPS;
        # 5-sec iterate × N open trades = rate-limit storm).
        if now - self._last_position_check_ts >= self.copy_settings.copy_position_check_seconds:
            await self._manage_open_positions()
            self._last_position_check_ts = now

    # ---- Redis pubsub subscriber (replaces polling) -----------------------

    async def _buys_subscriber(self) -> None:
        """Long-lived subscriber to `copy:buys`. Reconnects on disconnect.

        Webhook receiver service publishes WalletBuyEvent JSON dicts here.
        We feed each into the cluster detector — cluster firing is decoupled,
        runs from iterate().
        """
        if self._buys_redis is None:
            return
        backoff = 1.0
        while True:
            try:
                pubsub = self._buys_redis.pubsub()
                await pubsub.subscribe(REDIS_BUYS_CHANNEL)
                self.log.info("buys_subscriber_ready", channel=REDIS_BUYS_CHANNEL)
                backoff = 1.0
                async for msg in pubsub.listen():
                    if msg.get("type") != "message":
                        continue
                    try:
                        payload = json.loads(msg["data"])
                        ev = WalletBuyEvent(
                            wallet_address=payload["wallet_address"],
                            chain=payload["chain"],
                            token_mint=payload["token_mint"],
                            notional_usd=float(payload["notional_usd"]),
                            timestamp_ms=int(payload["timestamp_ms"]),
                            tx_signature=payload.get("tx_signature", ""),
                        )
                        self.cluster.observe_buy(ev)
                    except Exception:
                        self.log.exception("buys_message_parse_failed")
            except asyncio.CancelledError:
                return
            except Exception:
                self.log.exception("buys_subscriber_failed", backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def _evaluate_clusters(self) -> None:
        # Build A: no token-meta — gate is skipped per cluster.evaluate signature
        candidates = self.cluster.evaluate()
        for c in candidates:
            asyncio.create_task(self._consume_candidate(c))
            # Cross-bot bridge experiment (2026-05-25): if cluster is on a
            # macro asset (SOL/BTC/ETH) that has an HL perp market, publish
            # to copy:macro_cluster for STRUCTURE's passive subscriber.
            # Passive logging only — does not affect COPY's trading logic.
            hl_asset = MACRO_MINT_TO_HL_ASSET.get(c.asset)
            if hl_asset and self._buys_redis is not None:
                asyncio.create_task(self._publish_macro_cluster(c, hl_asset))

    async def _publish_macro_cluster(self, c: SignalCandidate, hl_asset: str) -> None:
        """Publish a macro cluster event for STRUCTURE to observe (experiment only)."""
        import uuid
        payload = json.dumps({
            "asset": hl_asset,
            "direction": c.direction,
            "wallet_count": c.cluster_size,
            "cluster_size_usd": c.payload.get("total_notional_usd", 0.0),
            "timestamp_ms": int(time() * 1000),
            "cluster_id": str(uuid.uuid4()),
        })
        try:
            await self._buys_redis.publish(REDIS_MACRO_CLUSTER_CHANNEL, payload)
            self.log.info(
                "macro_cluster_published",
                hl_asset=hl_asset,
                wallet_count=c.cluster_size,
            )
        except Exception:
            self.log.exception("macro_cluster_publish_failed")

    # ---- Signal handling ---------------------------------------------------

    async def _consume_candidate(self, candidate: SignalCandidate) -> None:
        if has_open_position(candidate.asset, candidate.venue):
            self.log.info("dedup_skip", asset=candidate.asset, chain=candidate.chain)
            return

        paper_capital = self.copy_settings.copy_paper_capital_usd
        current_alloc = open_allocation_pct(paper_capital)
        notional_usd = size_position(
            cluster_size=candidate.cluster_size,
            paper_capital_usd=paper_capital,
            current_open_alloc_pct=current_alloc,
            current_dd_today_pct=0.0,
        )
        if notional_usd <= 0:
            self.log.info("size_zero_skip",
                          cluster_size=candidate.cluster_size, current_alloc=current_alloc)
            return

        if self._session is None:
            return
        snapshot = CopyMarketSnapshot(chain=candidate.chain, session=self._session)
        sim_fill = await self.simulator.simulate_entry_async(
            asset=candidate.asset,
            notional_usd=notional_usd,
            leverage=1.0,
            direction=candidate.direction,
            market_snapshot=snapshot,
        )

        signal_id = persist_signal(candidate)
        persist_paper_trade(
            signal_id=signal_id,
            candidate=candidate,
            sim_fill=sim_fill,
            notional_usd=notional_usd,
            leverage=1.0,
        )

    # ---- Position management ----------------------------------------------

    async def _manage_open_positions(self) -> None:
        opens = list_open_paper_trades()
        if not opens or self._session is None:
            return

        now = datetime.now(timezone.utc)

        # Batch-fetch all Solana mints in ONE Birdeye call. Per-position
        # /defi/price was burning ~36 CU/min (12 opens × 3 CU × 60s cadence)
        # and exhausted the free-tier monthly quota in ~14h. /defi/multi_price
        # returns up to 100 prices in one request.
        sol_mints = sorted({t.asset for t in opens if t.venue == "solana"})
        sol_prices = await multi_price_solana(self._session, sol_mints) if sol_mints else {}

        # Flat-mark slippage estimate when we synthesize an exit fill (no
        # liquidity-aware impact data; matches the entry-side Birdeye estimate).
        EXIT_SLIPPAGE_BPS = 100.0

        evm_call_count = 0
        for trade in opens:
            # Resolve current mid price.
            mid: Optional[float] = None
            if trade.venue == "solana":
                mid = sol_prices.get(trade.asset)
            else:
                # EVM: per-trade 0x quote (no shared CU budget with Birdeye).
                # Pace between calls — first one fires immediately, subsequent
                # ones wait 1.2s.
                if evm_call_count > 0:
                    await asyncio.sleep(1.2)
                evm_call_count += 1
                try:
                    q = await quote(self._session, trade.venue, trade.asset, trade.size_usd)
                    if q is not None and q.expected_price_per_token_usd > 0:
                        mid = q.expected_price_per_token_usd
                except Exception:
                    self.log.warning("manage_quote_failed", asset=trade.asset)

            # Timeout is age-based — must fire even when pricing is unavailable
            # (rugged token, oracle gap, quota exhausted). Without this,
            # un-priceable positions live forever.
            timed_out = (
                trade.timeout_hours is not None
                and (now - trade.entry_at) >= timedelta(hours=trade.timeout_hours)
            )

            exit_reason: Optional[str] = None
            if mid is not None:
                pct_move = (mid - trade.entry_price) / trade.entry_price * 100.0
                if trade.direction == "short":
                    pct_move = -pct_move
                equity_pct = pct_move * trade.leverage
                if trade.stop_pct is not None and equity_pct <= -trade.stop_pct:
                    exit_reason = "stop"
                elif trade.take_profit_pct is not None and equity_pct >= trade.take_profit_pct:
                    exit_reason = "tp"
                elif timed_out:
                    exit_reason = "timeout"
            elif timed_out:
                exit_reason = "timeout_no_quote"

            if exit_reason is None:
                continue

            # Build the exit fill locally — we already have `mid` from the
            # batch, no need for the simulator to re-fetch it. When `mid` is
            # missing, flat-mark at entry_price (pnl=0) so the trade closes.
            if mid is not None:
                fees = trade.size_usd * (DEX_FEE_PCT / 100.0)
                exit_fill = SimulatedFill(
                    fill_price=mid,
                    fees_usd=fees,
                    slippage_bps=EXIT_SLIPPAGE_BPS,
                    metadata={"side": "exit", "chain": trade.venue,
                              "input_usd": trade.size_usd},
                )
                close_price = mid
            else:
                exit_fill = SimulatedFill(
                    fill_price=trade.entry_price,
                    fees_usd=0.0,
                    slippage_bps=0.0,
                    metadata={"side": "exit", "no_price_at_close": True},
                )
                close_price = trade.entry_price

            close_paper_trade(
                trade_id=trade.trade_id,
                exit_price=close_price,
                exit_fill=exit_fill,
                exit_reason=exit_reason,
            )

    # ---- Wallet pool -------------------------------------------------------

    def _load_wallet_pool(self) -> None:
        if not WALLET_POOL_PATH.exists():
            self.log.warning("wallet_pool_missing", path=str(WALLET_POOL_PATH))
            return
        try:
            data = json.loads(WALLET_POOL_PATH.read_text())
        except Exception:
            self.log.exception("wallet_pool_load_failed")
            return
        for entry in data.get("wallets", []) or []:
            addr = entry.get("address")
            chain = entry.get("chain")
            if not addr or not chain:
                continue
            if chain == "solana":
                self._wallets_solana.append(addr)
            elif chain == "base":
                self._wallets_base.append(addr)
            elif chain == "arbitrum":
                self._wallets_arbitrum.append(addr)


def main() -> None:
    bot = CopyBot()
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
