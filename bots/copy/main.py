"""COPY bot — main entry point.

Phase 2 Build A scope (paper-only, no DEX execution):
- Poll wallet pool every COPY_WALLET_POLL_SECONDS (Helius for Solana, Cielo for EVM)
- Feed buy events into the cluster detector
- On cluster trigger: signal → DEX-quoted sim fill → paper Trade
- Manage open paper positions (software stops, TP, timeout)
- Reconcile every COPY_LOOP_INTERVAL × N seconds (no-op until Build B)
- Per-bot DD halts via framework.dd_monitor cron (already wired)
- /panic listener via base class

Build B will add bots/copy/executor.py and ~10% shadow execution.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import time
from typing import Any, Optional

import aiohttp

from bots.base.bot_lifecycle import BotLifecycle
from bots.copy.config import get_copy_settings
from bots.copy.fill_simulator import CopyFillSimulator, CopyMarketSnapshot
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
from bots.copy.venue.cielo import CieloClient
from bots.copy.venue.dex_quoter import quote
from bots.copy.venue.helius_solana import HeliusSolanaClient, poll_wallets as poll_solana_wallets
from framework.alerts import emit_alert
from framework.config import get_settings as get_framework_settings
from framework.reconciliation import reconcile_once, register_venue_fetcher
from monitoring.alerting.taxonomy import Severity


WALLET_POOL_PATH = Path(__file__).parent / "wallet_pool.json"


class CopyBot(BotLifecycle):
    bot_id = "copy"

    def __init__(self) -> None:
        super().__init__()
        self.copy_settings = get_copy_settings()
        self.loop_interval_seconds = self.copy_settings.copy_loop_interval_seconds
        self.simulator = CopyFillSimulator()
        self.cluster = ClusterDetector()
        self.helius = HeliusSolanaClient()
        self.cielo = CieloClient()
        self._wallets_solana: list[str] = []
        self._wallets_base: list[str] = []
        self._wallets_arbitrum: list[str] = []
        self._last_wallet_poll_ts: float = 0.0
        self._last_reconcile_ts: float = 0.0
        self._sol_price_usd: float = 150.0
        self._session: Optional[aiohttp.ClientSession] = None

    # ---- Lifecycle hooks ---------------------------------------------------

    async def on_start(self) -> None:
        register_venue_fetcher("solana", make_fetcher_solana())
        register_venue_fetcher("base", make_fetcher_evm("base"))
        register_venue_fetcher("arbitrum", make_fetcher_evm("arbitrum"))
        self._load_wallet_pool()
        self._session = aiohttp.ClientSession()
        try:
            self._sol_price_usd = await self.helius.fetch_sol_price_usd(self._session)
        except Exception:
            self.log.warning("sol_price_fetch_failed_using_default", default=self._sol_price_usd)
        self.log.info(
            "copy_started",
            paper_capital_usd=self.copy_settings.copy_paper_capital_usd,
            sol_wallets=len(self._wallets_solana),
            base_wallets=len(self._wallets_base),
            arbitrum_wallets=len(self._wallets_arbitrum),
        )

    async def on_stop(self) -> None:
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

        # Wallet activity poll
        if now - self._last_wallet_poll_ts >= self.copy_settings.copy_wallet_poll_seconds:
            await self._poll_all_wallets()
            self._evaluate_clusters()
            self._last_wallet_poll_ts = now

        # Reconciliation cron
        recon_interval = get_framework_settings().reconciliation_interval_seconds
        if now - self._last_reconcile_ts >= recon_interval:
            try:
                await asyncio.to_thread(reconcile_once)
            except Exception:
                self.log.exception("reconcile_once_failed")
            self._last_reconcile_ts = now

        # Position management every iteration
        await self._manage_open_positions()

    # ---- Wallet polling ---------------------------------------------------

    async def _poll_all_wallets(self) -> None:
        if self._session is None:
            return
        events = []
        try:
            if self._wallets_solana:
                sol_events = await poll_solana_wallets(
                    self.helius, self._session, self._wallets_solana, self._sol_price_usd,
                )
                events.extend(sol_events)
        except Exception:
            self.log.exception("solana_poll_failed")
        try:
            for chain, wallets in (("base", self._wallets_base), ("arbitrum", self._wallets_arbitrum)):
                for w in wallets:
                    evs = await self.cielo.fetch_recent_buys(self._session, w, chain)
                    events.extend(evs)
        except Exception:
            self.log.exception("evm_poll_failed")

        for ev in events:
            self.cluster.observe_buy(ev)

    def _evaluate_clusters(self) -> None:
        # Build A: no token-meta — gate is skipped per cluster.evaluate signature
        candidates = self.cluster.evaluate()
        for c in candidates:
            asyncio.create_task(self._consume_candidate(c))

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

        for trade in opens:
            try:
                q = await quote(self._session, trade.venue, trade.asset, trade.size_usd)
            except Exception:
                self.log.warning("manage_quote_failed", asset=trade.asset)
                continue
            if q is None or q.expected_price_per_token_usd <= 0:
                continue
            mid = q.expected_price_per_token_usd
            pct_move = (mid - trade.entry_price) / trade.entry_price * 100.0
            if trade.direction == "short":
                pct_move = -pct_move
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

            snapshot = CopyMarketSnapshot(chain=trade.venue, session=self._session)
            exit_fill = await self.simulator.simulate_exit_async(
                asset=trade.asset,
                entry_price=trade.entry_price,
                exit_target_price=mid,
                notional_usd=trade.size_usd,
                leverage=trade.leverage,
                direction=trade.direction,
                market_snapshot=snapshot,
            )
            if exit_fill.fill_price is None:
                emit_alert(
                    severity=Severity.P2,
                    title="[copy] Could not simulate exit",
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
