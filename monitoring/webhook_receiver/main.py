"""COPY bot webhook receiver — Helius enhanced webhooks → Redis pubsub.

aiohttp web service. Single endpoint POST /copy/helius:
1. Validates `Authorization` header against HELIUS_WEBHOOK_AUTH_SECRET
2. Parses Helius's enhanced swap payload (array of events)
3. For each event, identifies which pool wallet participated (input side)
4. Builds a WalletBuyEvent JSON, dedups by tx_signature, publishes to
   `copy:buys` Redis channel
5. Returns 200 quickly (Helius retries on >10s response)

Also exposes GET /health for liveness checks.

Background task refreshes Solana SOL price every 5 min via Jupiter quote
(needed to convert wSOL/native swap amounts to USD).

Pool wallets loaded from bots/copy/wallet_pool.json on startup; receiver
must be restarted when pool changes (matches bot_copy's behavior).
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

from framework.logging_setup import configure_logging, get_logger


log = get_logger(__name__)

# ---- Constants ------------------------------------------------------------

WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
INPUT_LEG_MINTS = {WSOL_MINT, USDC_MINT, USDT_MINT}

REDIS_BUYS_CHANNEL = "copy:buys"
REDIS_DEDUP_PREFIX = "copy:webhook_dedup:"
DEDUP_TTL_SECONDS = 600  # 10 min — Helius retries within this window

WALLET_POOL_PATH = Path("/app/bots/copy/wallet_pool.json")

SOL_PRICE_REFRESH_SECONDS = 300  # 5 min
SOL_PRICE_FALLBACK_USD = 150.0


# ---- Helpers --------------------------------------------------------------

def _load_pool_addresses() -> set[str]:
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
    url = os.environ.get("JUPITER_QUOTE_URL", "https://quote-api.jup.ag/v6/quote")
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
    return web.json_response({"status": "ok", "pool_size": len(request.app["pool"])})


async def helius_webhook_handler(request: web.Request) -> web.Response:
    expected_secret: str = request.app["webhook_secret"]
    auth = request.headers.get("Authorization", "")
    if not expected_secret or not hmac.compare_digest(auth, expected_secret):
        log.warning("webhook_unauthorized", remote=request.remote)
        return web.Response(status=401, text="unauthorized")

    try:
        events = await request.json()
    except Exception:
        return web.Response(status=400, text="bad json")
    if not isinstance(events, list):
        events = [events]

    redis_client: redis_async.Redis = request.app["redis"]
    pool: set[str] = request.app["pool"]
    sol_price_usd: float = request.app.get("sol_price_usd") or SOL_PRICE_FALLBACK_USD

    matched = 0
    skipped_dup = 0
    skipped_unmatched = 0
    debug_unmatched = os.environ.get("COPY_RECEIVER_DEBUG_UNMATCHED", "0") == "1"
    for event in events:
        try:
            buy = _parse_swap_event(event, pool, sol_price_usd)
        except Exception:
            log.exception("event_parse_failed")
            continue
        if buy is None:
            skipped_unmatched += 1
            if debug_unmatched:
                # Log enough structure to diagnose why it didn't match
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
                         sig=(event.get("signature") or "")[:12],
                         src=event.get("source"),
                         type=event.get("type"),
                         has_swap=bool((event.get("events") or {}).get("swap")),
                         tts=tt_summary, nts=nt_summary)
            continue

        # Dedup by tx_signature — Helius may retry on prior failures
        sig = buy["tx_signature"]
        if sig:
            dedup_key = f"{REDIS_DEDUP_PREFIX}{sig}"
            seen = await redis_client.set(dedup_key, "1", nx=True, ex=DEDUP_TTL_SECONDS)
            if not seen:  # already processed
                skipped_dup += 1
                continue

        try:
            await redis_client.publish(REDIS_BUYS_CHANNEL, json.dumps(buy))
            matched += 1
        except Exception:
            log.exception("redis_publish_failed")

    log.info("webhook_batch",
             total=len(events), matched=matched,
             skipped_dup=skipped_dup, skipped_unmatched=skipped_unmatched)
    return web.Response(status=200, text="ok")


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
    app["pool"] = _load_pool_addresses()
    app["sol_price_usd"] = SOL_PRICE_FALLBACK_USD

    log.info("webhook_receiver_starting",
             pool_size=len(app["pool"]),
             auth_configured=bool(secret))

    app.router.add_get("/health", health_handler)
    app.router.add_post("/copy/helius", helius_webhook_handler)

    async def _start_bg(_app: web.Application) -> None:
        _app["sol_price_task"] = asyncio.create_task(_sol_price_refresher(_app))

    async def _cleanup(_app: web.Application) -> None:
        task = _app.get("sol_price_task")
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
