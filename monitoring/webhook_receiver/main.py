"""COPY bot webhook receiver — Helius enhanced webhooks → Redis pubsub.

aiohttp web service with TWO endpoints (per wallet tier):
- POST /copy/helius        → active tier (publishes to copy:buys Redis channel)
- POST /copy/helius/watch  → watch tier (NO Redis publish; event-log only)

Both endpoints:
1. Validate `Authorization` header against HELIUS_WEBHOOK_AUTH_SECRET
2. Parse Helius's enhanced swap payload (array of events)
3. For each event, identify which pool wallet participated (input side)
4. Write a wallet_events_log row + update wallet_pool.last_event_at
5. ACTIVE ONLY: build a WalletBuyEvent JSON, dedup by tx_signature, publish to
   `copy:buys` Redis channel
6. Return 200 quickly (Helius retries on >10s response)

The active/watch split exists so we can subscribe to ~500 "watch" wallets
cheaply (Helius charges per delivered event — dormant wallets cost $0)
while only the ~75 "active" wallets feed cluster detection. Daily cron
promotes watch wallets that have shown ≥5 swaps/7d AND demotes active
wallets with <10 events/30d. See scripts/wallet_pool_daily_cron.py.

Also exposes GET /health for liveness checks.

Pool tiers loaded from `wallet_pool` table on startup; refreshed every 60s
in the background so tier changes from the daily cron take effect without
receiver restart.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

import aiohttp
import redis.asyncio as redis_async
from aiohttp import web

from sqlalchemy import select, text

from framework.db import session_scope
from framework.logging_setup import configure_logging, get_logger
from framework.models import WalletEventLog, WalletPool, WalletSwapLog


log = get_logger(__name__)

POOL_REFRESH_SECONDS = 60

# ---- Constants ------------------------------------------------------------

WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
# Other stablecoins + LST tokens to skip as buy outputs (not alpha signal).
# Keep in sync with bots/copy/venue/helius_solana.py.
USD1_MINT = "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB"
PYUSD_MINT = "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo"
JITOSOL_MINT = "J1toSO1tj92gWhpaCftRkhZmDz5G8b8AqfeATfDVfo7"
MSOL_MINT = "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So"
BSOL_MINT = "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1"
INPUT_LEG_MINTS = {
    WSOL_MINT, USDC_MINT, USDT_MINT,
    USD1_MINT, PYUSD_MINT,
    JITOSOL_MINT, MSOL_MINT, BSOL_MINT,
}

REDIS_BUYS_CHANNEL = "copy:buys"
# Team-follow experiment: teamfollow-tier wallet BUYS publish here (isolated from
# copy:buys) so they never feed the cluster detector. Buys only (no sell stream).
REDIS_TEAMFOLLOW_BUYS_CHANNEL = "copy:teamfollow_buys"
# Team-follow SELLS publish here (isolated from copy:sells so they never feed
# the cluster/conviction detectors) — powers the follow-the-team-out exit.
REDIS_TEAMFOLLOW_SELLS_CHANNEL = "copy:teamfollow_sells"
REDIS_SELLS_CHANNEL = "copy:sells"
REDIS_DEDUP_PREFIX = "copy:webhook_dedup:"
DEDUP_TTL_SECONDS = 600  # 10 min — Helius retries within this window

# Real webhook-DELIVERY tally (every event Helius sends ≈ 1 webhookDelivery credit,
# whether or not it matched a pool wallet). wallet_events_log only holds MATCHED buys
# (~6% of deliveries), so it badly under-counts credit burn. These cheap per-tier daily
# Redis counters give credit_pool_snapshot the TRUE burn. Keyed by UTC day; ~45d TTL.
HELIUS_DELIV_PREFIX = "helius:deliv:"          # helius:deliv:{YYYY-MM-DD}:{tier}
HELIUS_DELIV_TTL_SECONDS = 45 * 24 * 3600      # rolling ~45d history (covers a cycle)

WALLET_POOL_PATH = Path("/app/bots/copy/wallet_pool.json")

SOL_PRICE_REFRESH_SECONDS = 300  # 5 min
SOL_PRICE_FALLBACK_USD = 150.0


# ---- Helpers --------------------------------------------------------------

def _load_pool_addresses() -> set[str]:
    """Legacy JSON loader — kept as fallback for bootstrap before the
    migration script populates the wallet_pool table.
    """
    if not WALLET_POOL_PATH.exists():
        log.warning("wallet_pool_missing", path=str(WALLET_POOL_PATH))
        return set()
    try:
        doc = json.loads(WALLET_POOL_PATH.read_text())
    except Exception:
        log.exception("wallet_pool_load_failed")
        return set()
    out: set[str] = set()
    for entry in doc.get("wallets", []) or []:
        addr = entry.get("address")
        chain = entry.get("chain")
        if addr and chain == "solana":
            out.add(addr)
    return out


def _load_pool_tiers_from_db() -> tuple[set[str], set[str], set[str]]:
    """Return (active_pool, watch_pool, teamfollow_pool) of Solana addresses.

    Pruned wallets are NOT subscribed to any webhook so they're excluded.
    teamfollow = the isolated team-follow experiment tier (its buys publish to
    copy:teamfollow_buys, NOT copy:buys, so they never feed the cluster detector).
    """
    active: set[str] = set()
    watch: set[str] = set()
    teamfollow: set[str] = set()
    try:
        with session_scope() as s:
            for w in s.execute(select(WalletPool).where(
                WalletPool.chain == "solana",
                WalletPool.tier.in_(("active", "watch", "teamfollow")),
            )).scalars():
                if w.tier == "active":
                    active.add(w.address)
                elif w.tier == "watch":
                    watch.add(w.address)
                elif w.tier == "teamfollow":
                    teamfollow.add(w.address)
    except Exception:
        log.exception("wallet_pool_db_load_failed")
    return active, watch, teamfollow


async def _pool_refresher(app: web.Application) -> None:
    """Background refresh of active/watch pool sets every POOL_REFRESH_SECONDS.

    Lets daily promote/demote cron take effect without receiver restart.
    """
    while True:
        try:
            active, watch, teamfollow = await asyncio.to_thread(_load_pool_tiers_from_db)
            # If DB is empty (e.g. pre-migration), keep using the JSON-loaded
            # legacy pool as active. This is the bootstrap fallback.
            if active or watch or teamfollow:
                app["active_pool"] = active
                app["watch_pool"] = watch
                app["teamfollow_pool"] = teamfollow
                log.info("pool_refreshed",
                         active=len(active), watch=len(watch), teamfollow=len(teamfollow))
        except Exception:
            log.exception("pool_refresh_failed")
        await asyncio.sleep(POOL_REFRESH_SECONDS)


def _persist_event_batch(
    matched_events: list[tuple[str, str]],
    swap_rows: Optional[list[dict]] = None,
) -> None:
    """Write wallet_events_log rows + bump wallet_pool counters + wallet_swaps_log.

    matched_events: list of (wallet_address, source_webhook_tier).
    swap_rows: list of WalletSwapLog mappings (wallet+token+side+notional+time) —
      the raw per-swap log WITH the token, for multi-day slow-cluster research.
    Both persist in one transaction. Runs sync; called via asyncio.to_thread.
    """
    if not matched_events and not swap_rows:
        return
    now = datetime_now_utc()
    try:
        with session_scope() as s:
            if matched_events:
                # Bulk insert event log rows
                s.bulk_insert_mappings(WalletEventLog, [
                    {"wallet_address": addr, "event_at": now, "source_webhook": tier}
                    for addr, tier in matched_events
                ])
                # Update each affected wallet's last_event_at + events_total.
                # Aggregate by address first to minimize UPDATE statements.
                counts: dict[str, int] = {}
                for addr, _ in matched_events:
                    counts[addr] = counts.get(addr, 0) + 1
                for addr, n in counts.items():
                    s.execute(text(
                        "UPDATE wallet_pool SET last_event_at = :now, "
                        "events_total = events_total + :n WHERE address = :addr"
                    ), {"now": now, "n": n, "addr": addr})
            # Raw per-swap log (buys + sells) with the token — shadow research data
            # for the slow-cluster (multi-day group accumulation) signal.
            if swap_rows:
                s.bulk_insert_mappings(WalletSwapLog, swap_rows)
    except Exception:
        log.exception("persist_event_batch_failed",
                      n=len(matched_events), swaps=len(swap_rows or []))


def datetime_now_utc():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _ms_to_dt(timestamp_ms: int):
    """On-chain swap timestamp (ms) -> tz-aware datetime. Falls back to now()
    on any parse error (so a bad timestamp never drops the swap row)."""
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _parse_swap_event(
    event: dict,
    pool: set[str],
    sol_price_usd: float,
) -> Optional[dict]:
    """Helius enhanced swap event → WalletBuyEvent dict (Redis-publishable).

    Returns None if no pool wallet was the buyer.

    Tries two parsers in order:
    1. Primary: structured `events.swap` field (Jupiter, Raydium, Orca, etc.)
    2. Fallback: tokenTransfers + nativeTransfers analysis (PUMP_FUN, and
       any other DEX where Helius doesn't populate events.swap).
    """
    ts = event.get("timestamp")
    if not ts or int(ts) <= 0:
        return None
    timestamp_ms = int(ts) * 1000
    swap = (event.get("events") or {}).get("swap")
    if swap:
        out = _parse_via_events_swap(event, swap, pool, sol_price_usd, timestamp_ms)
        if out is not None:
            return out
    return _parse_via_transfers(event, pool, sol_price_usd, timestamp_ms)


def _parse_sell_event(
    event: dict,
    pool: set[str],
    sol_price_usd: float,
) -> Optional[dict]:
    """Helius enhanced swap event → WalletSellEvent dict (sell side).

    Mirror of _parse_swap_event but identifies the opposite swap leg:
    pool wallet SENDS a non-stable token and RECEIVES SOL/USDC/USDT.

    Returns None if no pool wallet was the seller. Same two-parser
    fallback structure (events.swap primary, transfers fallback).
    """
    ts = event.get("timestamp")
    if not ts or int(ts) <= 0:
        return None
    timestamp_ms = int(ts) * 1000
    swap = (event.get("events") or {}).get("swap")
    if swap:
        out = _parse_sell_via_events_swap(event, swap, pool, sol_price_usd, timestamp_ms)
        if out is not None:
            return out
    return _parse_sell_via_transfers(event, pool, sol_price_usd, timestamp_ms)


def _parse_sell_via_events_swap(
    event: dict,
    swap: dict,
    pool: set[str],
    sol_price_usd: float,
    timestamp_ms: int,
) -> Optional[dict]:
    token_inputs = swap.get("tokenInputs") or []
    token_outputs = swap.get("tokenOutputs") or []
    native_output = swap.get("nativeOutput") or {}

    for wallet in pool:
        # Did this wallet SEND a non-stable token? That's the "sold" leg.
        sold_mint: Optional[str] = None
        for ti in token_inputs:
            user = ti.get("userAccount") or ti.get("fromUserAccount")
            if user != wallet:
                continue
            mint = ti.get("mint")
            if not mint or mint in INPUT_LEG_MINTS:
                continue
            sold_mint = mint
            break
        if not sold_mint:
            continue

        # Sum USDC/USDT/SOL OUTFLOW to this wallet (the proceeds).
        proceeds_usd = 0.0
        for to in token_outputs:
            user = to.get("userAccount") or to.get("toUserAccount")
            if user != wallet:
                continue
            mint = to.get("mint")
            amt_str = (to.get("rawTokenAmount") or {}).get("tokenAmount") or "0"
            decimals = (to.get("rawTokenAmount") or {}).get("decimals") or 0
            try:
                ui_amount = float(amt_str) / (10 ** int(decimals))
            except Exception:
                continue
            if mint in (USDC_MINT, USDT_MINT):
                proceeds_usd += ui_amount
            elif mint == WSOL_MINT:
                proceeds_usd += ui_amount * sol_price_usd

        if native_output.get("account") == wallet:
            try:
                lamports = int(native_output.get("amount", 0))
                proceeds_usd += (lamports / 1_000_000_000) * sol_price_usd
            except Exception:
                pass

        if proceeds_usd <= 0:
            continue

        return {
            "wallet_address": wallet,
            "chain": "solana",
            "token_mint": sold_mint,
            "notional_usd": proceeds_usd,
            "timestamp_ms": timestamp_ms,
            "tx_signature": event.get("signature", ""),
        }
    return None


def _parse_sell_via_transfers(
    event: dict,
    pool: set[str],
    sol_price_usd: float,
    timestamp_ms: int,
) -> Optional[dict]:
    """Transfer-based sell parser, mirror of _parse_via_transfers buy logic.

    SELL pattern: wallet SENDS a non-stable token AND RECEIVES SOL/USDC/USDT.
    """
    token_transfers = event.get("tokenTransfers") or []
    native_transfers = event.get("nativeTransfers") or []

    for wallet in pool:
        # Did this wallet SEND a non-stable token?
        sold_mint: Optional[str] = None
        for tt in token_transfers:
            if tt.get("fromUserAccount") != wallet:
                continue
            mint = tt.get("mint")
            if not mint or mint in INPUT_LEG_MINTS:
                continue
            sold_mint = mint
            break
        if not sold_mint:
            continue

        # If they ALSO received a non-stable token, this is a token-for-token
        # rotation, not a "cashing out" sell. Skip to avoid double-counting
        # against the buy detector (which also processes this event).
        received_non_stable = any(
            tt.get("toUserAccount") == wallet and tt.get("mint") not in INPUT_LEG_MINTS
            for tt in token_transfers
        )
        if received_non_stable:
            continue

        # Sum SOL inflow to wallet (lamports → USD)
        lamports_in = 0
        for nt in native_transfers:
            if nt.get("toUserAccount") == wallet:
                try:
                    lamports_in += int(nt.get("amount", 0))
                except Exception:
                    pass
        proceeds_usd = (lamports_in / 1_000_000_000) * sol_price_usd

        # Also sum wrapped-SOL + stablecoin inflows.
        for tt in token_transfers:
            if tt.get("toUserAccount") != wallet:
                continue
            mint = tt.get("mint")
            try:
                amt = float(tt.get("tokenAmount") or 0)
            except Exception:
                continue
            if mint == WSOL_MINT:
                proceeds_usd += amt * sol_price_usd
            elif mint in (USDC_MINT, USDT_MINT):
                proceeds_usd += amt

        if proceeds_usd <= 0:
            continue

        return {
            "wallet_address": wallet,
            "chain": "solana",
            "token_mint": sold_mint,
            "notional_usd": proceeds_usd,
            "timestamp_ms": timestamp_ms,
            "tx_signature": event.get("signature", ""),
        }
    return None


def _parse_via_events_swap(
    event: dict,
    swap: dict,
    pool: set[str],
    sol_price_usd: float,
    timestamp_ms: int,
) -> Optional[dict]:
    token_inputs = swap.get("tokenInputs") or []
    token_outputs = swap.get("tokenOutputs") or []
    native_input = swap.get("nativeInput") or {}

    for wallet in pool:
        notional_usd = 0.0
        for ti in token_inputs:
            user = ti.get("userAccount") or ti.get("fromUserAccount")
            if user != wallet:
                continue
            mint = ti.get("mint")
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

        if native_input.get("account") == wallet:
            try:
                lamports = int(native_input.get("amount", 0))
                notional_usd += (lamports / 1_000_000_000) * sol_price_usd
            except Exception:
                pass

        if notional_usd <= 0:
            continue

        bought_mint: Optional[str] = None
        for to in token_outputs:
            user = to.get("userAccount") or to.get("toUserAccount")
            if user != wallet:
                continue
            mint = to.get("mint")
            if mint in INPUT_LEG_MINTS:
                continue
            bought_mint = mint
            break

        if not bought_mint:
            continue

        return {
            "wallet_address": wallet,
            "chain": "solana",
            "token_mint": bought_mint,
            "notional_usd": notional_usd,
            "timestamp_ms": timestamp_ms,
            "tx_signature": event.get("signature", ""),
        }
    return None


def _parse_via_transfers(
    event: dict,
    pool: set[str],
    sol_price_usd: float,
    timestamp_ms: int,
) -> Optional[dict]:
    """Fallback for sources that don't populate events.swap (e.g. PUMP_FUN).

    BUY pattern observed empirically on PUMP_FUN bonding-curve buys:
    - tokenTransfers: wallet receives 1 non-stable token (toUserAccount=wallet)
    - nativeTransfers: wallet sends multiple SOL outflows (fees + cost)
    SELL pattern (we skip): wallet sends a non-stable token AND/OR doesn't
    receive a non-stable token.
    """
    token_transfers = event.get("tokenTransfers") or []
    native_transfers = event.get("nativeTransfers") or []

    for wallet in pool:
        # Did this wallet RECEIVE a non-stable token?
        bought_mint: Optional[str] = None
        for tt in token_transfers:
            if tt.get("toUserAccount") != wallet:
                continue
            mint = tt.get("mint")
            if not mint or mint in INPUT_LEG_MINTS:
                continue
            bought_mint = mint
            break
        if not bought_mint:
            continue

        # If they ALSO sent a non-stable token, this is a token-for-token
        # swap (not a "smart-money buying with cash") — skip.
        sent_non_stable = any(
            tt.get("fromUserAccount") == wallet and tt.get("mint") not in INPUT_LEG_MINTS
            for tt in token_transfers
        )
        if sent_non_stable:
            continue

        # Sum SOL outflow from this wallet (native lamports → USD)
        lamports_out = 0
        for nt in native_transfers:
            if nt.get("fromUserAccount") == wallet:
                try:
                    lamports_out += int(nt.get("amount", 0))
                except Exception:
                    pass

        notional_usd = (lamports_out / 1_000_000_000) * sol_price_usd

        # Also sum wrapped SOL + stablecoin outflows (DEX aggregators often
        # wrap SOL into wSOL before swapping, so the spend appears in
        # tokenTransfers with mint=So11111111... not nativeTransfers).
        for tt in token_transfers:
            if tt.get("fromUserAccount") != wallet:
                continue
            mint = tt.get("mint")
            try:
                amt = float(tt.get("tokenAmount") or 0)
            except Exception:
                continue
            if mint == WSOL_MINT:
                notional_usd += amt * sol_price_usd
            elif mint in (USDC_MINT, USDT_MINT):
                notional_usd += amt

        if notional_usd <= 0:
            continue

        return {
            "wallet_address": wallet,
            "chain": "solana",
            "token_mint": bought_mint,
            "notional_usd": notional_usd,
            "timestamp_ms": timestamp_ms,
            "tx_signature": event.get("signature", ""),
        }
    return None


# ---- Background SOL price refresher --------------------------------------

async def _fetch_sol_price(session: aiohttp.ClientSession) -> float:
    # Default migrated 2026-06-08 from quote-api.jup.ag/v6/quote to the
    # lite-api endpoint. Env var override preserved for ops flexibility.
    url = os.environ.get("JUPITER_QUOTE_URL", "https://lite-api.jup.ag/swap/v1/quote")
    params = {
        "inputMint": WSOL_MINT,
        "outputMint": USDC_MINT,
        "amount": "1000000000",  # 1 SOL
        "slippageBps": "100",
    }
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status != 200:
                return SOL_PRICE_FALLBACK_USD
            data = await r.json()
        out = int(data.get("outAmount", 0))
        return out / 1_000_000 if out > 0 else SOL_PRICE_FALLBACK_USD
    except Exception:
        return SOL_PRICE_FALLBACK_USD


async def _sol_price_refresher(app: web.Application) -> None:
    session = app["http"]
    while True:
        try:
            price = await _fetch_sol_price(session)
            app["sol_price_usd"] = price
            log.info("sol_price_refreshed", usd=price)
        except Exception:
            log.exception("sol_price_refresh_failed")
        await asyncio.sleep(SOL_PRICE_REFRESH_SECONDS)


# ---- HTTP handlers --------------------------------------------------------

async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "active_pool_size": len(request.app["active_pool"]),
        "watch_pool_size": len(request.app["watch_pool"]),
        "teamfollow_pool_size": len(request.app.get("teamfollow_pool", set())),
    })


async def _process_webhook(
    request: web.Request,
    tier: str,
) -> web.Response:
    """Shared handler body for both /copy/helius and /copy/helius/watch.

    tier: 'active' | 'watch'
      active → uses active_pool; publishes matched buys to copy:buys Redis channel
      watch  → uses watch_pool;  no Redis publish (event-log only)
    Both write to wallet_events_log + bump wallet_pool counters.
    """
    expected_secret: str = request.app["webhook_secret"]
    auth = request.headers.get("Authorization", "")
    if not expected_secret or not hmac.compare_digest(auth, expected_secret):
        log.warning("webhook_unauthorized", tier=tier, remote=request.remote)
        return web.Response(status=401, text="unauthorized")

    try:
        events = await request.json()
    except Exception:
        return web.Response(status=400, text="bad json")
    if not isinstance(events, list):
        events = [events]

    redis_client: redis_async.Redis = request.app["redis"]
    pool: set[str] = request.app[f"{tier}_pool"]
    sol_price_usd: float = request.app.get("sol_price_usd") or SOL_PRICE_FALLBACK_USD

    matched_buys = 0
    matched_sells = 0
    skipped_dup = 0
    skipped_unmatched = 0
    log_rows: list[tuple[str, str]] = []  # (wallet_address, tier) for batch persist
    swap_rows: list[dict] = []  # raw per-swap log (wallet+token+side+notional+time)
    debug_unmatched = os.environ.get("COPY_RECEIVER_DEBUG_UNMATCHED", "0") == "1"

    for event in events:
        # Try buy parse first, then sell parse. A single event could
        # plausibly produce a sell of token A + buy of token B (token-for-
        # token rotation), but our parsers skip that case on the buy side
        # via `sent_non_stable` filter and on the sell side via
        # `received_non_stable` filter — so each event resolves to at
        # most one side classification.
        try:
            buy = _parse_swap_event(event, pool, sol_price_usd)
        except Exception:
            log.exception("buy_event_parse_failed", tier=tier)
            buy = None
        sell: Optional[dict] = None
        if buy is None:
            try:
                sell = _parse_sell_event(event, pool, sol_price_usd)
            except Exception:
                log.exception("sell_event_parse_failed", tier=tier)
                sell = None

        if buy is None and sell is None:
            skipped_unmatched += 1
            if debug_unmatched:
                tts = event.get("tokenTransfers") or []
                nts = event.get("nativeTransfers") or []
                tt_summary = [
                    {"to": (t.get("toUserAccount") or "")[:10], "from": (t.get("fromUserAccount") or "")[:10],
                     "mint": (t.get("mint") or "")[:10], "amt": t.get("tokenAmount")}
                    for t in tts[:6]
                ]
                nt_summary = [
                    {"to": (n.get("toUserAccount") or "")[:10], "from": (n.get("fromUserAccount") or "")[:10],
                     "amt": n.get("amount")}
                    for n in nts[:6]
                ]
                log.info("unmatched_event",
                         tier=tier,
                         sig=(event.get("signature") or "")[:12],
                         src=event.get("source"),
                         type=event.get("type"),
                         has_swap=bool((event.get("events") or {}).get("swap")),
                         tts=tt_summary, nts=nt_summary)
            continue

        matched_event = buy or sell  # exactly one is truthy here
        kind = "buy" if buy is not None else "sell"
        if tier == "teamfollow":
            channel = (REDIS_TEAMFOLLOW_BUYS_CHANNEL if buy is not None
                       else REDIS_TEAMFOLLOW_SELLS_CHANNEL)
        else:
            channel = REDIS_BUYS_CHANNEL if buy is not None else REDIS_SELLS_CHANNEL
        log_rows.append((matched_event["wallet_address"], tier))
        # Raw per-swap log WITH the token (buys + sells, both tiers) — passive
        # research data for the slow-cluster (multi-day group accumulation) signal.
        swap_rows.append({
            "wallet_address": matched_event["wallet_address"],
            "chain": matched_event.get("chain", "solana"),
            "token_mint": matched_event["token_mint"],
            "side": kind,
            "notional_usd": matched_event["notional_usd"],
            "source_webhook": tier,
            "tx_signature": matched_event.get("tx_signature") or None,
            "event_at": _ms_to_dt(matched_event["timestamp_ms"]),
        })

        # Publish for active (buys+sells) and teamfollow (buys → follow-in signal,
        # sells → follow-the-team-out exit, on separate channels). Dedup key is
        # tier+kind namespaced so cross-tier/side deliveries never collide.
        should_publish = (tier == "active") or (tier == "teamfollow")
        if should_publish:
            sig = matched_event["tx_signature"]
            if sig:
                dedup_key = f"{REDIS_DEDUP_PREFIX}{tier}:{kind}:{sig}"
                seen = await redis_client.set(dedup_key, "1", nx=True, ex=DEDUP_TTL_SECONDS)
                if not seen:
                    skipped_dup += 1
                    continue
            try:
                await redis_client.publish(channel, json.dumps(matched_event))
                if kind == "buy":
                    matched_buys += 1
                else:
                    matched_sells += 1
            except Exception:
                log.exception("redis_publish_failed", tier=tier, kind=kind)
        else:
            if kind == "buy":
                matched_buys += 1
            else:
                matched_sells += 1

    # Persist event log + bump counters + raw swap log in one batched DB roundtrip
    if log_rows or swap_rows:
        try:
            await asyncio.to_thread(_persist_event_batch, log_rows, swap_rows)
        except Exception:
            log.exception("event_log_persist_failed", tier=tier, n=len(log_rows))

    # Tally TRUE deliveries (all events, matched or not) for accurate credit
    # accounting. Fail-safe: a Redis error here must never affect ingestion.
    if events:
        try:
            day = datetime_now_utc().strftime("%Y-%m-%d")
            dkey = f"{HELIUS_DELIV_PREFIX}{day}:{tier}"
            await redis_client.incrby(dkey, len(events))
            await redis_client.expire(dkey, HELIUS_DELIV_TTL_SECONDS)
        except Exception:
            log.debug("delivery_tally_failed", tier=tier)

    log.info("webhook_batch",
             tier=tier,
             total=len(events), matched_buys=matched_buys, matched_sells=matched_sells,
             skipped_dup=skipped_dup, skipped_unmatched=skipped_unmatched)
    return web.Response(status=200, text="ok")


async def helius_webhook_handler(request: web.Request) -> web.Response:
    return await _process_webhook(request, tier="active")


async def helius_watch_webhook_handler(request: web.Request) -> web.Response:
    return await _process_webhook(request, tier="watch")


async def helius_teamfollow_webhook_handler(request: web.Request) -> web.Response:
    return await _process_webhook(request, tier="teamfollow")


# ---- App lifecycle --------------------------------------------------------

async def init_app() -> web.Application:
    app = web.Application()

    secret = os.environ.get("HELIUS_WEBHOOK_AUTH_SECRET", "")
    if not secret:
        log.warning("webhook_auth_secret_unset",
                    note="all incoming requests will be rejected")
    app["webhook_secret"] = secret

    redis_url = os.environ.get(
        "REDIS_URL",
        f"redis://{os.environ.get('REDIS_HOST', 'redis')}:{os.environ.get('REDIS_PORT', '6379')}/0",
    )
    app["redis"] = redis_async.from_url(redis_url, decode_responses=True)

    app["http"] = aiohttp.ClientSession()
    # Bootstrap: try DB first, fall back to JSON if empty (pre-migration state).
    active, watch, teamfollow = _load_pool_tiers_from_db()
    if not active and not watch:
        legacy = _load_pool_addresses()
        log.info("wallet_pool_db_empty_fallback_json", n=len(legacy))
        active = legacy
        watch = set()
    app["active_pool"] = active
    app["watch_pool"] = watch
    app["teamfollow_pool"] = teamfollow
    app["sol_price_usd"] = SOL_PRICE_FALLBACK_USD

    log.info("webhook_receiver_starting",
             active_pool=len(active),
             watch_pool=len(watch),
             auth_configured=bool(secret))

    app.router.add_get("/health", health_handler)
    app.router.add_post("/copy/helius", helius_webhook_handler)
    app.router.add_post("/copy/helius/watch", helius_watch_webhook_handler)
    app.router.add_post("/copy/helius/teamfollow", helius_teamfollow_webhook_handler)

    async def _start_bg(_app: web.Application) -> None:
        _app["sol_price_task"] = asyncio.create_task(_sol_price_refresher(_app))
        _app["pool_refresh_task"] = asyncio.create_task(_pool_refresher(_app))

    async def _cleanup(_app: web.Application) -> None:
        for k in ("sol_price_task", "pool_refresh_task"):
            task = _app.get(k)
            if task:
                task.cancel()
        await _app["http"].close()
        await _app["redis"].aclose()

    app.on_startup.append(_start_bg)
    app.on_cleanup.append(_cleanup)
    return app


def main() -> None:
    configure_logging()
    port = int(os.environ.get("WEBHOOK_RECEIVER_PORT", "8090"))
    log.info("webhook_receiver_listening", port=port)
    web.run_app(init_app(), host="0.0.0.0", port=port, print=lambda *_: None)


if __name__ == "__main__":
    main()
