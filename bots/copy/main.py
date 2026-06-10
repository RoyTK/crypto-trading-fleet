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
    execute_paper_partial_close,
    has_open_position,
    list_open_paper_trades,
    list_open_real_trades,
    open_allocation_pct,
    panic_close_all_open,
    persist_paper_trade,
    persist_signal,
    update_trade_peak_pct,
    update_trade_token_age,
    update_trade_token_meta,
    write_cluster_detection,
)
from bots.copy.executor import CopyExecutor
from bots.copy.trailing_stop import evaluate_exit_actions, is_price_scale_anomaly
from bots.copy.reconciliation import make_fetcher_evm, make_fetcher_solana
from bots.copy.signals.base import SignalCandidate
from bots.copy.signals.cluster import ClusterDetector
from bots.copy.signals.sell_cluster import SellClusterDetector
from bots.copy import shadow_log
from bots.copy.loop_helpers import _classify_cluster_wallet_tier
from bots.copy.sizing import size_position
from bots.copy.venue.dex_quoter import (
    fetch_token_creation,
    fetch_token_security,
    multi_price_solana,
    quote,
)
from bots.copy.venue.helius_solana import WalletBuyEvent, WalletSellEvent
from bots.copy.venue.solana_wallet import is_wallet_available, public_key_b58
from framework.alerts import emit_alert
from framework.config import get_settings as get_framework_settings
from framework.reconciliation import reconcile_once, register_venue_fetcher
from monitoring.alerting.taxonomy import Severity


REDIS_BUYS_CHANNEL = "copy:buys"
REDIS_SELLS_CHANNEL = "copy:sells"
REDIS_MACRO_CLUSTER_CHANNEL = "copy:macro_cluster"
# 2026-05-28: every cluster (regardless of asset / HL listing) is published
# to copy:all_clusters for STRUCTURE's read-only cluster_observations journal.
# Pure logging — does NOT reset STRUCTURE's kill-criteria window.
REDIS_ALL_CLUSTERS_CHANNEL = "copy:all_clusters"

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


def _completed_tier_indexes(
    trade_id: int,
    partial_tiers: tuple[tuple[float, float], ...],
) -> tuple[int, ...]:
    """Read the already-filled partial-exit tier INDEXES from
    Trade.sim_metadata. Used by the exit-evaluator so a tier that
    already executed doesn't re-fire on every cycle.

    Identity is tier_index (0..N-1), NOT tier_pct. This means changing
    PARTIAL_EXIT_TIERS values mid-flight is always safe — a position
    that fired the old "tier 0 at 200%" stays marked tier 0 even if
    the config now says tier 0 is at 300%.

    Falls back to inferring index from tier_pct for legacy records
    that don't carry tier_index (shouldn't happen in new data; defensive
    for hypothetical future replays).
    """
    from framework.db import session_scope
    from framework.models import Trade
    try:
        with session_scope() as s:
            t = s.get(Trade, trade_id)
            if t is None:
                return ()
            md = t.sim_metadata or {}
            partials = md.get("partial_exits") or []
        out: set[int] = set()
        for p in partials:
            if not isinstance(p, dict) or p.get("status") != "filled":
                continue
            idx = p.get("tier_index")
            if idx is not None:
                try:
                    out.add(int(idx))
                except (TypeError, ValueError):
                    pass
                continue
            # Legacy fallback: infer from tier_pct against current ladder
            tier_pct = p.get("tier_pct")
            if tier_pct is None:
                continue
            try:
                pct_val = float(tier_pct)
            except (TypeError, ValueError):
                continue
            for i, (t_pct, _) in enumerate(partial_tiers):
                if abs(t_pct - pct_val) < 0.001:
                    out.add(i)
                    break
        return tuple(sorted(out))
    except Exception:
        return ()


def _extract_wallet_list(c: SignalCandidate) -> list[str]:
    """Normalize the wallets payload to a flat list of address strings.

    Cluster.evaluate emits payload["wallets"] sometimes as dict keyed by
    address (with notional values) and sometimes as a list. This helper
    returns a list either way.
    """
    wallets_payload = (c.payload or {}).get("wallets") or {}
    if isinstance(wallets_payload, dict):
        return list(wallets_payload.keys())
    if isinstance(wallets_payload, list):
        return list(wallets_payload)
    return []


def _cluster_wallet_tier(c: SignalCandidate) -> str:
    """Extract wallets from candidate.payload + classify via wallet_pool tier."""
    return _classify_cluster_wallet_tier(_extract_wallet_list(c))


class CopyBot(BotLifecycle):
    bot_id = "copy"

    def __init__(self) -> None:
        super().__init__()
        self.copy_settings = get_copy_settings()
        self.loop_interval_seconds = self.copy_settings.copy_loop_interval_seconds
        self.simulator = CopyFillSimulator()
        self.cluster = ClusterDetector()
        self.sell_cluster = SellClusterDetector()
        self.executor = CopyExecutor()
        self._wallets_solana: list[str] = []
        self._wallets_base: list[str] = []
        self._wallets_arbitrum: list[str] = []
        self._last_reconcile_ts: float = 0.0
        self._last_position_check_ts: float = 0.0
        self._last_shadow_log_poll_ts: float = 0.0
        self._session: Optional[aiohttp.ClientSession] = None
        self._buys_subscriber_task: Optional[asyncio.Task] = None
        self._sells_subscriber_task: Optional[asyncio.Task] = None
        self._buys_redis: Optional[redis_async.Redis] = None
        # Throttle for price-scale anomaly alerts. Per-trade, 1 hour
        # cool-down — prevents spam if Birdeye is returning persistently
        # bad data and every cycle re-triggers the guard for the same trade.
        self._anomaly_alert_ts: dict[int, float] = {}

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
        # Sell-cluster subscriber — shares the same Redis connection. Pubsub
        # objects are created per-subscriber so they coexist without
        # interfering. Gated by copy_sell_cluster_enabled (default true).
        if self.copy_settings.copy_sell_cluster_enabled:
            self._sells_subscriber_task = asyncio.create_task(self._sells_subscriber())
        # Visibility on executor configuration at startup. Paper-only is the
        # default state; flipping copy_live_enabled (with a wallet present)
        # is what graduates to shadow execution.
        self.log.info(
            "copy_started",
            paper_capital_usd=self.copy_settings.copy_paper_capital_usd,
            sol_wallets=len(self._wallets_solana),
            base_wallets=len(self._wallets_base),
            arbitrum_wallets=len(self._wallets_arbitrum),
            live_enabled=self.copy_settings.copy_live_enabled,
            live_full_enabled=self.copy_settings.copy_live_full_enabled,
            wallet_available=is_wallet_available(),
            wallet_pubkey=public_key_b58(),
        )

    async def on_stop(self) -> None:
        if self._buys_subscriber_task:
            self._buys_subscriber_task.cancel()
        if self._sells_subscriber_task:
            self._sells_subscriber_task.cancel()
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
        # Sell-cluster: defensive exit signal. Fires when 2+ active wallets
        # sell the same token in 15 min → close any open positions in that
        # token. Per brainstorm 2026-05-30: "sell-cluster as LONG-SIDE STOPS
        # first." Independent state from buy cluster; same dedup primitive.
        self._evaluate_sell_clusters()

        # Shadow log poller — update pending rows on the configured cadence
        if now - self._last_shadow_log_poll_ts >= self.copy_settings.copy_shadow_log_poll_seconds:
            try:
                await self._poll_shadow_log()
            except Exception:
                self.log.exception("shadow_log_poll_failed")
            self._last_shadow_log_poll_ts = now

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

    async def _sells_subscriber(self) -> None:
        """Long-lived subscriber to `copy:sells`. Mirrors `_buys_subscriber`.

        Webhook receiver publishes WalletSellEvent JSON dicts on this channel.
        We feed each into the sell-cluster detector — firing is decoupled and
        runs from iterate() via _evaluate_sell_clusters.
        """
        if self._buys_redis is None:
            return
        backoff = 1.0
        while True:
            try:
                pubsub = self._buys_redis.pubsub()
                await pubsub.subscribe(REDIS_SELLS_CHANNEL)
                self.log.info("sells_subscriber_ready", channel=REDIS_SELLS_CHANNEL)
                backoff = 1.0
                async for msg in pubsub.listen():
                    if msg.get("type") != "message":
                        continue
                    try:
                        payload = json.loads(msg["data"])
                        ev = WalletSellEvent(
                            wallet_address=payload["wallet_address"],
                            chain=payload["chain"],
                            token_mint=payload["token_mint"],
                            notional_usd=float(payload["notional_usd"]),
                            timestamp_ms=int(payload["timestamp_ms"]),
                            tx_signature=payload.get("tx_signature", ""),
                        )
                        self.sell_cluster.observe_sell(ev)
                    except Exception:
                        self.log.exception("sells_message_parse_failed")
            except asyncio.CancelledError:
                return
            except Exception:
                self.log.exception("sells_subscriber_failed", backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def _evaluate_clusters(self) -> None:
        # Build A: no token-meta — gate is skipped per cluster.evaluate signature
        candidates = self.cluster.evaluate()
        for c in candidates:
            cluster_uuid = shadow_log.make_cluster_uuid()
            wallet_tier = _cluster_wallet_tier(c)

            # Persistent dedup gate (2026-05-30). Atomic upsert keyed on
            # (chain, token, signal_type, direction, window_bucket). Replaces
            # what the cluster detector's 15-min in-memory _already_fired
            # only partially handled — bot restarts cleared that state and
            # cross-day re-fires on the same token slipped through. See
            # decision log entry 2026-05-30 (unique_pct=37.5% diagnostic).
            try:
                dedup = write_cluster_detection(
                    candidate=c,
                    cluster_uuid=cluster_uuid,
                    wallet_tier=wallet_tier,
                    dedup_hours=self.copy_settings.copy_cluster_dedup_hours,
                )
            except Exception:
                self.log.exception(
                    "cluster_detection_write_failed",
                    asset=c.asset, chain=c.chain,
                )
                # Fail-open: if the dedup primitive crashes, prefer to emit
                # the signal rather than silently swallow it. The data-quality
                # cost of one duplicate is lower than the cost of dropping a
                # real signal during a DB hiccup.
                dedup = None

            if dedup is not None and not dedup.fired:
                self.log.info(
                    "cluster_dedup_suppressed",
                    asset=c.asset, chain=c.chain,
                    cluster_size=c.cluster_size,
                    reason=dedup.reason,
                    dedup_hours=self.copy_settings.copy_cluster_dedup_hours,
                )
                continue

            # Persist the full signal payload to shadow_signals so the
            # wallet list survives even when COPY_CLUSTER_BUY_ENABLED=false
            # (the live `signals` table is only written when the bot trades).
            # Shipped 2026-06-04 after the ABGVN investigation revealed all 4
            # mega-winner signals had NULL wallet info.
            wallet_list = _extract_wallet_list(c)
            asyncio.create_task(asyncio.to_thread(
                shadow_log.write_shadow_signal,
                cluster_uuid=cluster_uuid,
                bot_id="copy",
                signal_type=c.signal_type,
                asset=c.asset,
                chain=c.chain,
                direction=c.direction,
                cluster_size=c.cluster_size,
                cluster_wallets=wallet_list,
                payload=dict(c.payload or {}),
            ))

            # Always: shadow log every fire (H1/H2 diagnostic). Independent
            # of whether bot actually trades. Caller injects the cluster_uuid
            # so the same id is shared with downstream Redis publishes.
            asyncio.create_task(self._write_shadow_log(c, cluster_uuid, wallet_list))

            # Always: publish to copy:all_clusters for STRUCTURE journal
            if self._buys_redis is not None:
                asyncio.create_task(self._publish_all_cluster(c, cluster_uuid))

            # Cross-bot bridge: macro subset for backward compat
            hl_asset = MACRO_MINT_TO_HL_ASSET.get(c.asset)
            if hl_asset and self._buys_redis is not None:
                asyncio.create_task(self._publish_macro_cluster(c, hl_asset, cluster_uuid))

            # Trade only if cluster_buy is enabled (paused 2026-05-28 by
            # the data-driven correction carve-out — adverse signal H2
            # confirmed by statistician's H3 rejection).
            if self.copy_settings.copy_cluster_buy_enabled:
                asyncio.create_task(self._consume_candidate(c))
            else:
                self.log.info("cluster_buy_paused_skip_trade",
                              asset=c.asset, cluster_size=c.cluster_size)

    async def _publish_macro_cluster(self, c: SignalCandidate, hl_asset: str, cluster_uuid: str) -> None:
        """Publish a macro cluster event for STRUCTURE to observe (experiment only)."""
        payload = json.dumps({
            "asset": hl_asset,
            "direction": c.direction,
            "wallet_count": c.cluster_size,
            "cluster_size_usd": c.payload.get("total_notional_usd", 0.0),
            "timestamp_ms": int(time() * 1000),
            "cluster_id": cluster_uuid,
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

    async def _publish_all_cluster(self, c: SignalCandidate, cluster_uuid: str) -> None:
        """Publish EVERY cluster (not just macro) to copy:all_clusters.
        STRUCTURE journals these to cluster_observations — read-only research.
        """
        hl_asset = MACRO_MINT_TO_HL_ASSET.get(c.asset)
        payload = json.dumps({
            "cluster_uuid": cluster_uuid,
            "token_mint": c.asset,
            "hl_asset_if_any": hl_asset,
            "cluster_size": c.cluster_size,
            "cluster_size_usd": c.payload.get("total_notional_usd", 0.0),
            "wallet_tier": _cluster_wallet_tier(c),
            "timestamp_ms": int(time() * 1000),
        })
        try:
            await self._buys_redis.publish(REDIS_ALL_CLUSTERS_CHANNEL, payload)
        except Exception:
            self.log.exception("all_cluster_publish_failed")

    async def _write_shadow_log(
        self, c: SignalCandidate, cluster_uuid: str,
        cluster_wallets: Optional[list[str]] = None,
    ) -> None:
        """Fire-time shadow log entry. entry_price fetched async via existing
        Birdeye batch endpoint. cluster_wallets stored for post-hoc wallet
        attribution even while bot is paused."""
        hl_asset = MACRO_MINT_TO_HL_ASSET.get(c.asset)
        # Attempt an entry-price quote via multi_price_solana (existing batch).
        entry_price: Optional[float] = None
        try:
            if self._session is not None:
                prices = await multi_price_solana(self._session, [c.asset])
                entry_price = prices.get(c.asset)
        except Exception:
            self.log.warning("shadow_log_entry_price_fetch_failed", asset=c.asset)
        await asyncio.to_thread(
            shadow_log.write_fire,
            cluster_uuid=cluster_uuid,
            signal_id=None,  # signals.id is set in _consume_candidate path; not available here when paused
            token_mint=c.asset,
            hl_asset_if_any=hl_asset,
            cluster_size=c.cluster_size,
            cluster_total_notional_usd=float(c.payload.get("total_notional_usd", 0.0)),
            wallet_tier=_cluster_wallet_tier(c),
            entry_price=entry_price,
            cluster_wallets=cluster_wallets if cluster_wallets is not None else _extract_wallet_list(c),
        )

    async def _poll_shadow_log(self) -> None:
        """Update pending shadow_log rows with current prices + MFE/MAE."""
        if self._session is None:
            return
        rows = await asyncio.to_thread(shadow_log._select_pending)
        if not rows:
            return
        # Batch Birdeye price fetch for all distinct mints
        mints = list({r["token_mint"] for r in rows})
        try:
            prices = await multi_price_solana(self._session, mints)
        except Exception:
            self.log.warning("shadow_log_poll_price_batch_failed", n_mints=len(mints))
            return
        for r in rows:
            cur = prices.get(r["token_mint"])
            updates = shadow_log.update_one(r, cur)
            if updates:
                await asyncio.to_thread(shadow_log.write_updates, r["id"], updates)

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

        # Token security lookup (Solana only): one Birdeye call that serves
        # BOTH the serial-deployer blocklist (skip the buy) AND concentration
        # instrumentation (stored post-placement). Best-effort / fail-open:
        # a failed lookup never blocks a buy and just leaves the fields unset.
        token_security: Optional[dict] = None
        if candidate.venue == "solana":
            try:
                token_security = await fetch_token_security(self._session, candidate.asset)
            except Exception:
                self.log.exception("token_security_fetch_failed", asset=candidate.asset)
            if token_security:
                creator = token_security.get("creator")
                blocked = self.copy_settings.get_blocked_creators()
                if creator and creator in blocked:
                    self.log.info("blocked_creator_skip",
                                  asset=candidate.asset, creator=creator,
                                  cluster_size=candidate.cluster_size)
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
        paper_trade_id = self.executor.place_paper(
            signal_id=signal_id,
            candidate=candidate,
            sim_fill=sim_fill,
            notional_usd=notional_usd,
            leverage=1.0,
        )
        # Executor-driven shadow + live placement (both gated by config and
        # never able to block the paper path). Both calls are no-ops when
        # copy_live_enabled=false, which is the default until Roy provisions
        # the keypair + .env on Hetzner.
        if paper_trade_id is not None and self._session is not None:
            try:
                await self.executor.maybe_place_shadow(
                    session=self._session,
                    signal_id=signal_id,
                    paper_trade_id=paper_trade_id,
                    candidate=candidate,
                    paper_sim_fill=sim_fill,
                    paper_notional_usd=notional_usd,
                )
            except Exception:
                self.log.exception("executor_shadow_failed",
                                   asset=candidate.asset, signal_id=signal_id)
            try:
                await self.executor.maybe_place_live(
                    session=self._session,
                    signal_id=signal_id,
                    paper_trade_id=paper_trade_id,
                    candidate=candidate,
                    paper_sim_fill=sim_fill,
                    notional_usd=notional_usd,
                )
            except Exception:
                self.log.exception("executor_live_failed",
                                   asset=candidate.asset, signal_id=signal_id)

            # Best-effort token-age capture. Runs LAST (after paper +
            # shadow + live placement) so it can never delay or block an
            # entry fill. Stamps sim_metadata.token_age_at_entry_hours so
            # we can study rug risk by token age — rugs cluster in the
            # first minutes-to-hours of a fresh mint (research 2026-06-10:
            # median Solana rug lifespan ~17 min). Solana-only for now
            # (Birdeye token_creation_info is the source).
            if candidate.venue == "solana":
                # Concentration instrumentation — store the security data we
                # already fetched pre-placement (no second call). Pure
                # instrumentation; we do NOT filter on concentration.
                if token_security is not None:
                    try:
                        update_trade_token_meta(
                            paper_trade_id,
                            creator=token_security.get("creator"),
                            top10_holder_pct=token_security.get("top10_holder_pct"),
                            owner_pct=token_security.get("owner_pct"),
                        )
                        self.log.info("token_security_captured",
                                      asset=candidate.asset,
                                      creator=token_security.get("creator"),
                                      top10_holder_pct=token_security.get("top10_holder_pct"))
                    except Exception:
                        self.log.exception("token_security_capture_failed",
                                           asset=candidate.asset)
                try:
                    import time
                    info = await fetch_token_creation(self._session, candidate.asset)
                    if info and info.get("created_unix"):
                        age_hours = max(0.0, (time.time() - info["created_unix"]) / 3600.0)
                        update_trade_token_age(
                            paper_trade_id,
                            created_unix=info["created_unix"],
                            age_hours=age_hours,
                            tx=info.get("tx"),
                        )
                        self.log.info("token_age_captured",
                                      asset=candidate.asset,
                                      age_hours=round(age_hours, 3))
                    else:
                        self.log.info("token_age_unavailable", asset=candidate.asset)
                except Exception:
                    self.log.exception("token_age_capture_failed",
                                       asset=candidate.asset)

    # ---- Position management ----------------------------------------------

    def _emit_anomaly_alert(self, trade: Any, current_price: float, *, side: str) -> None:
        """Throttled P2 Discord alert when the price-scale guardrail fires.

        Throttle is per-trade for 1 hour. If Birdeye returns persistently
        bad data for a token, we want ONE alert + structured logs every
        cycle — not a Discord storm.
        """
        import time
        last = self._anomaly_alert_ts.get(trade.trade_id, 0.0)
        now = time.time()
        if now - last < 3600:
            return
        self._anomaly_alert_ts[trade.trade_id] = now
        try:
            ratio = current_price / trade.entry_price if trade.entry_price > 0 else float("inf")
            emit_alert(
                severity=Severity.P2,
                title=f"[copy] price-scale anomaly on trade {trade.trade_id} ({side})",
                body=(
                    f"Position management for trade {trade.trade_id} skipped this cycle.\n"
                    f"Asset: `{trade.asset}` ({trade.venue})\n"
                    f"Entry price: {trade.entry_price}\n"
                    f"Current price (Birdeye): {current_price}\n"
                    f"Implied ratio: {ratio:.2g}x (guardrail >10,000x)\n\n"
                    "Almost certainly a price-source bug, not a real move. "
                    "Investigate the underlying mint pricing (Birdeye / Jupiter / "
                    "decimals lookup). Position stays open; partials WILL NOT fire "
                    "while the ratio remains anomalous."
                ),
                bot_id="copy",
                event_type="price_scale_anomaly_skip",
                metadata={
                    "trade_id": trade.trade_id,
                    "asset": trade.asset,
                    "venue": trade.venue,
                    "side": side,
                    "entry_price": trade.entry_price,
                    "current_price": current_price,
                    "implied_ratio": ratio,
                },
            )
        except Exception:
            self.log.exception("anomaly_alert_emit_failed", trade_id=trade.trade_id)

    async def _manage_open_positions(self) -> None:
        opens = list_open_paper_trades()
        # Drive shadow/live exits in parallel with the paper-trade exits below.
        # The real path swaps the held token back to USDC via Jupiter — slow,
        # IO-bound, so we kick it off and don't block the paper loop on it.
        await self._manage_open_real_trades()
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
            partial_actions: list = []
            if mid is not None and trade.entry_price > 0:
                # Price-scale anomaly guardrail: if entry/current ratio
                # is absurd (>10,000x), refuse to act this cycle. The
                # underlying cause is almost certainly a price-source bug
                # (wrong-units math, oracle glitch). Don't update peak,
                # don't fire partials, don't close. Position waits for
                # sane prices. Caught the 2026-06-09 cbBTC cascade.
                if is_price_scale_anomaly(trade.entry_price, mid):
                    self.log.warning(
                        "price_scale_anomaly_skip",
                        trade_id=trade.trade_id,
                        asset=trade.asset,
                        entry_price=trade.entry_price,
                        current_price=mid,
                        implied_ratio=mid / trade.entry_price,
                        venue=trade.venue,
                        side="paper",
                    )
                    self._emit_anomaly_alert(trade, mid, side="paper")
                    continue
                # New tiered ladder + multiplicative trailing. Returns
                # (peak, partials_to_fire, full_close_reason_or_None).
                # Partials are sold synthetically (paper) below; full
                # close routes through close_paper_trade as before.
                ladder = self.copy_settings.get_partial_exit_tiers()
                completed = _completed_tier_indexes(trade.trade_id, ladder)
                new_peak, partial_actions, exit_reason = evaluate_exit_actions(
                    entry_price=trade.entry_price,
                    current_price=mid,
                    stored_peak_pct=trade.peak_pct_since_entry,
                    completed_tier_indexes=completed,
                    partial_tiers=ladder,
                    leverage=trade.leverage,
                    direction=trade.direction,
                    stop_pct=trade.stop_pct,
                    take_profit_pct=trade.take_profit_pct,
                )
                if trade.peak_pct_since_entry is None or new_peak > trade.peak_pct_since_entry:
                    try:
                        update_trade_peak_pct(trade.trade_id, new_peak)
                    except Exception:
                        self.log.exception("paper_peak_persist_failed",
                                           trade_id=trade.trade_id)
                # Fire each partial tier that should execute this cycle.
                for action in partial_actions:
                    try:
                        execute_paper_partial_close(
                            trade_id=trade.trade_id,
                            tier_pct=action.tier_pct,
                            fraction=action.fraction,
                            tier_index=action.tier_index,
                            current_price=mid,
                        )
                    except Exception:
                        self.log.exception("paper_partial_close_failed",
                                           trade_id=trade.trade_id,
                                           tier_pct=action.tier_pct)
                # If the final tier fired in this cycle, the position is
                # fully exited via partials — close the row with 'tier_complete'.
                if (len(completed) + len(partial_actions)) >= len(ladder):
                    exit_reason = "tier_complete"
                if exit_reason is None and timed_out:
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

    # ---- Sell-cluster evaluation + position close --------------------------

    def _evaluate_sell_clusters(self) -> None:
        """Drain candidates from the sell-cluster detector and act on each.

        For each fired sell-cluster (post-dedup), kick off a background
        coroutine to close any open positions in the candidate's token —
        paper trades via the simulator, shadow/live via the executor.
        Like the buy-cluster path, the dedup primitive prevents duplicate
        fires across the configured window.
        """
        if not self.copy_settings.copy_sell_cluster_enabled:
            return
        candidates = self.sell_cluster.evaluate()
        for c in candidates:
            cluster_uuid = shadow_log.make_cluster_uuid()
            wallet_tier = _cluster_wallet_tier(c)

            # Record the detection for analytics, but DO NOT gate the exit
            # action on dedup (Option A, 2026-06-10). Exit clusters must
            # fire on EVERY wave — an escalating sell sequence is more
            # conviction to get out, not a duplicate to ignore. The 15-min
            # in-memory guard in SellClusterDetector.evaluate() already
            # prevents fire-storms within a session, and re-firing an exit
            # is a harmless no-op when no position is open
            # (_close_positions_on_sell_cluster logs sell_cluster_no_open_positions).
            #
            # The prior 24h DB dedup (shared with BUY clusters) silently
            # suppressed the real exit waves on TRILL (trade 719) and NUT
            # (trade 729) — both rugged shortly after. A pre-entry exit
            # cluster consumed the 24h slot, so the post-entry "smart money
            # is leaving" wave was suppressed and the position was never
            # closed. Only the +200% hard cap saved those two; the new
            # ladder removes that cap, so a working per-wave sell-cluster
            # is now the primary rug backstop.
            try:
                dedup = write_cluster_detection(
                    candidate=c,
                    cluster_uuid=cluster_uuid,
                    wallet_tier=wallet_tier,
                    dedup_hours=self.copy_settings.copy_cluster_dedup_hours,
                )
                if dedup is not None and not dedup.fired:
                    self.log.info(
                        "sell_cluster_dedup_dup_acting_anyway",
                        asset=c.asset, chain=c.chain,
                        cluster_size=c.cluster_size,
                        reason=dedup.reason,
                    )
            except Exception:
                self.log.exception("sell_cluster_detection_write_failed",
                                   asset=c.asset, chain=c.chain)

            # Persist shadow signal so we have a record even when no
            # position exists to close (most fires will land here —
            # the cohort sells lots of tokens we don't hold).
            wallet_list = _extract_wallet_list(c)
            asyncio.create_task(asyncio.to_thread(
                shadow_log.write_shadow_signal,
                cluster_uuid=cluster_uuid,
                bot_id="copy",
                signal_type=c.signal_type,
                asset=c.asset,
                chain=c.chain,
                direction=c.direction,
                cluster_size=c.cluster_size,
                cluster_wallets=wallet_list,
                payload=dict(c.payload or {}),
            ))

            # Action: close any open positions in this token.
            asyncio.create_task(self._close_positions_on_sell_cluster(c))

    async def _close_positions_on_sell_cluster(self, c: SignalCandidate) -> None:
        """Close all open paper + shadow + live positions matching the
        sell-cluster's asset. Best-effort: individual close failures are
        logged but don't block the other positions.
        """
        paper_trades = [t for t in list_open_paper_trades()
                        if t.asset == c.asset and t.venue == c.venue]
        real_trades = [t for t in list_open_real_trades()
                       if t.asset == c.asset and t.venue == c.venue]
        if not paper_trades and not real_trades:
            self.log.info(
                "sell_cluster_no_open_positions",
                asset=c.asset, cluster_size=c.cluster_size,
            )
            return
        self.log.warning(
            "sell_cluster_close_triggered",
            asset=c.asset, cluster_size=c.cluster_size,
            paper_count=len(paper_trades), real_count=len(real_trades),
        )
        # Resolve current mid for paper close PnL math. If unavailable,
        # mark at entry_price (pnl=0) — better than holding indefinitely
        # when the cohort is rotating out.
        mid: Optional[float] = None
        if paper_trades and self._session is not None and c.chain == "solana":
            try:
                prices = await multi_price_solana(self._session, [c.asset])
                mid = prices.get(c.asset)
            except Exception:
                self.log.warning("sell_cluster_paper_price_fetch_failed",
                                 asset=c.asset)

        # Constant for the synthesized paper exit. Matches the
        # _manage_open_positions cadence — DEX-paper world is fee-only.
        EXIT_SLIPPAGE_BPS_PAPER = 100.0
        for t in paper_trades:
            close_price = mid if mid is not None else t.entry_price
            fees = float(t.size_usd) * (DEX_FEE_PCT / 100.0)
            exit_fill = SimulatedFill(
                fill_price=close_price,
                fees_usd=fees,
                slippage_bps=EXIT_SLIPPAGE_BPS_PAPER,
                metadata={"side": "exit", "exit_reason": "sell_cluster",
                          "no_price_at_close": mid is None},
            )
            try:
                close_paper_trade(
                    trade_id=t.trade_id,
                    exit_price=close_price,
                    exit_fill=exit_fill,
                    exit_reason="sell_cluster",
                )
            except Exception:
                self.log.exception("sell_cluster_paper_close_failed",
                                   trade_id=t.trade_id, asset=c.asset)

        # Close shadow + live via real on-chain swap.
        if real_trades and self._session is not None:
            for t in real_trades:
                try:
                    await self.executor.close_real_trade(
                        session=self._session,
                        trade_id=t.trade_id,
                        exit_reason="sell_cluster",
                    )
                except Exception:
                    self.log.exception("sell_cluster_real_close_failed",
                                       trade_id=t.trade_id, mode=t.mode,
                                       asset=c.asset)

    # ---- Real-trade (shadow/live) exit loop --------------------------------

    async def _manage_open_real_trades(self) -> None:
        """Drive exits for open shadow/live trades.

        Reuses the same stop/TP/timeout logic as paper, but on hit we route
        through the executor (which swaps the token back to USDC on-chain)
        instead of writing a simulated close. Runs every position-check
        interval, same cadence as paper exits.

        Like the paper loop, this is best-effort: any per-trade exception is
        caught and logged so a single failing trade doesn't block the others.
        """
        if self._session is None:
            return
        opens = list_open_real_trades()
        if not opens:
            return
        # Batch-fetch all Solana mids in one Birdeye call (same optimization
        # the paper loop uses — see comment in _manage_open_positions).
        sol_mints = sorted({t.asset for t in opens if t.venue == "solana"})
        sol_prices = await multi_price_solana(self._session, sol_mints) if sol_mints else {}
        now = datetime.now(timezone.utc)
        for trade in opens:
            try:
                mid = sol_prices.get(trade.asset) if trade.venue == "solana" else None
                timed_out = (
                    trade.timeout_hours is not None
                    and (now - trade.entry_at) >= timedelta(hours=trade.timeout_hours)
                )
                exit_reason: Optional[str] = None
                partial_actions: list = []
                if mid is not None and trade.entry_price > 0:
                    # Same price-scale anomaly guardrail as the paper
                    # path. Extra critical here because the shadow/live
                    # path actually swaps tokens on-chain — a bogus
                    # cascade would burn real SOL on real-bad fills.
                    if is_price_scale_anomaly(trade.entry_price, mid):
                        self.log.warning(
                            "price_scale_anomaly_skip",
                            trade_id=trade.trade_id,
                            asset=trade.asset,
                            entry_price=trade.entry_price,
                            current_price=mid,
                            implied_ratio=mid / trade.entry_price,
                            venue=trade.venue,
                            side="real",
                        )
                        self._emit_anomaly_alert(trade, mid, side="real")
                        continue
                    # Same tiered ladder + trailing as the paper path.
                    # Partials route through executor.execute_partial_close
                    # (real Jupiter swap of the tier's fraction).
                    ladder = self.copy_settings.get_partial_exit_tiers()
                    completed = _completed_tier_indexes(trade.trade_id, ladder)
                    new_peak, partial_actions, exit_reason = evaluate_exit_actions(
                        entry_price=trade.entry_price,
                        current_price=mid,
                        stored_peak_pct=trade.peak_pct_since_entry,
                        completed_tier_indexes=completed,
                        partial_tiers=ladder,
                        leverage=1.0,  # shadow/live are spot 1x
                        direction=trade.direction,
                        stop_pct=trade.stop_pct,
                        take_profit_pct=trade.take_profit_pct,
                    )
                    if trade.peak_pct_since_entry is None or new_peak > trade.peak_pct_since_entry:
                        try:
                            update_trade_peak_pct(trade.trade_id, new_peak)
                        except Exception:
                            self.log.exception("real_peak_persist_failed",
                                               trade_id=trade.trade_id)
                    # Fire each partial tier. A failure (e.g. Jupiter
                    # quote unavailable for the partial size) doesn't
                    # mark the tier completed — caller will retry next
                    # cycle. We still proceed to the next tier this cycle
                    # to capture any that DO work, since they're independent
                    # swaps.
                    for action in partial_actions:
                        try:
                            await self.executor.execute_partial_close(
                                session=self._session,
                                trade_id=trade.trade_id,
                                tier_pct=action.tier_pct,
                                fraction=action.fraction,
                                tier_index=action.tier_index,
                            )
                        except Exception:
                            self.log.exception("real_partial_close_failed",
                                               trade_id=trade.trade_id,
                                               tier_pct=action.tier_pct)
                    if (len(completed) + len(partial_actions)) >= len(ladder):
                        exit_reason = "tier_complete"
                    if exit_reason is None and timed_out:
                        exit_reason = "timeout"
                elif timed_out:
                    # No mid available — still fire timeout so positions don't
                    # live forever when the oracle has gone dark.
                    exit_reason = "timeout_no_quote"
                if exit_reason is None:
                    continue
                await self.executor.close_real_trade(
                    session=self._session,
                    trade_id=trade.trade_id,
                    exit_reason=exit_reason,
                )
            except Exception:
                self.log.exception(
                    "manage_real_trade_failed",
                    trade_id=trade.trade_id, mode=trade.mode,
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
