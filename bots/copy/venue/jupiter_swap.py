"""Jupiter aggregator swap client — quote + build + sign + send + confirm.

Existing `dex_quoter.py` covers the QUOTE half (price discovery). This
module covers the EXECUTION half: turn a quote into a signed Solana
transaction, submit it, and wait for confirmation.

Flow:
  1. quote_for_swap(input_mint, output_mint, amount_in_atomic) → JupiterQuote
     - Same Jupiter endpoint as dex_quoter, but returns the full quote
       blob (we need to pass it back to /swap).
  2. build_swap_transaction(quote_response, user_pubkey) → base64 tx
     - POST to Jupiter /swap with the quote + user's pubkey. Jupiter
       returns a placeholder-signed VersionedTransaction.
  3. solana_wallet.sign_versioned_transaction(tx_b64) → signed_tx_b64
     - We sign in place.
  4. send_transaction(signed_tx_b64) → signature
     - Submit to Solana RPC (Helius).
  5. confirm_transaction(signature, timeout_sec) → SwapResult
     - Poll getSignatureStatus until confirmed/finalized/dropped.
     - Parse meta for actual amount-in / amount-out to derive fill price.

Failure modes:
  - Quote times out / 429 → retry once, then return None
  - Insufficient SOL for gas → error from RPC
  - Slippage exceeded → tx fails on-chain (logged but no chain charges
    other than priority-fee SOL burn)
  - Tx dropped before confirmation → return DROPPED status

Cost: 1 Jupiter quote + 1 Jupiter swap + 1 RPC send + ~5-10 RPC poll
calls per swap. Helius free tier 10M credits/mo amply covers this.
"""
from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

from bots.copy.config import get_copy_settings
from bots.copy.venue.solana_wallet import (
    is_wallet_available,
    public_key_b58,
    sign_versioned_transaction,
)
from framework.logging_setup import get_logger

log = get_logger(__name__)

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# USDC has 6 decimals everywhere we care about.
USDC_DECIMALS = 6


@dataclass
class JupiterQuote:
    """Raw Jupiter /quote response + the parsed fields we need to act on."""
    input_mint: str
    output_mint: str
    in_amount_atomic: int
    out_amount_atomic: int
    other_amount_threshold_atomic: int
    price_impact_pct: float
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SwapResult:
    """Outcome of a Jupiter swap attempt.

    status is one of: 'filled', 'failed', 'dropped', 'no_wallet', 'rejected'
    - filled: confirmed on-chain, fill_price + actual_out_atomic populated
    - failed: tx confirmed but reported error (e.g. slippage exceeded)
    - dropped: tx never confirmed within timeout
    - no_wallet: no signing key configured — caller gated wrong
    - rejected: pre-flight failure (quote unavailable, signing failed, etc.)

    attempt_index + slippage_bps_used tell which ladder tier produced this
    result. Useful telemetry to see whether most fills happen at the
    tight 200bps tier (liquid tokens) or the loose 3000bps tier (memecoins).
    """
    status: str
    signature: Optional[str] = None
    fill_price_usd: Optional[float] = None       # USD per token bought (or sold)
    actual_in_atomic: Optional[int] = None
    actual_out_atomic: Optional[int] = None
    slippage_bps: Optional[float] = None          # REALIZED slippage from on-chain
    fees_usd: Optional[float] = None
    error_message: Optional[str] = None
    raw_meta: Optional[dict[str, Any]] = None
    attempt_index: int = 0                        # 0-indexed ladder tier
    slippage_bps_used: int = 0                    # tolerance used on this attempt


# Reasons we should retry at a wider slippage. 'quote_unavailable' means
# Jupiter couldn't find a route at the current tolerance; widening MAY help.
# 'failed' and 'dropped' are also retryable — the tx executed but slippage
# breached or the network dropped it; both can be tolerance-related.
# Everything else (no_wallet, signing_failed, etc.) is terminal — wider
# slippage doesn't help when the bot can't sign at all.
_RETRYABLE_REJECTED_REASONS = frozenset({"quote_unavailable"})


def should_escalate(result: SwapResult) -> bool:
    """Decide whether to advance to the next slippage tier.

    Pure function for testability. The ladder iterator calls this between
    attempts; True means continue, False means return immediately.
    """
    if result.status in ("filled", "no_wallet"):
        return False
    if result.status == "rejected":
        return (result.error_message or "") in _RETRYABLE_REJECTED_REASONS
    # 'failed' (slippage breach during execution) and 'dropped' (no inclusion
    # within timeout) — both can plausibly be fixed by widening tolerance.
    return result.status in ("failed", "dropped")


async def quote_for_swap(
    session: aiohttp.ClientSession,
    input_mint: str,
    output_mint: str,
    amount_in_atomic: int,
    slippage_bps: int,
) -> Optional[JupiterQuote]:
    """Fetch a Jupiter v6 quote. Returns the full quote object (caller
    passes raw back to /swap), or None on failure."""
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_in_atomic),
        "slippageBps": str(slippage_bps),
    }
    try:
        async with session.get(
            JUPITER_QUOTE_URL, params=params,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status != 200:
                log.warning("jupiter_quote_failed",
                            status=r.status, in_mint=input_mint, out_mint=output_mint)
                return None
            data = await r.json()
    except Exception:
        log.exception("jupiter_quote_exception",
                      in_mint=input_mint, out_mint=output_mint)
        return None

    try:
        return JupiterQuote(
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount_atomic=int(data.get("inAmount", 0)),
            out_amount_atomic=int(data.get("outAmount", 0)),
            other_amount_threshold_atomic=int(data.get("otherAmountThreshold", 0)),
            price_impact_pct=float(data.get("priceImpactPct", 0) or 0),
            raw=data,
        )
    except Exception:
        log.exception("jupiter_quote_parse_failed", in_mint=input_mint, out_mint=output_mint)
        return None


async def build_swap_transaction(
    session: aiohttp.ClientSession,
    quote: JupiterQuote,
    user_pubkey_b58: str,
    priority_fee_lamports: int,
) -> Optional[str]:
    """POST to Jupiter /swap. Returns base64 VersionedTransaction or None."""
    body = {
        "quoteResponse": quote.raw,
        "userPublicKey": user_pubkey_b58,
        # Jupiter recommends true for memecoin trades — auto-creates the
        # destination ATA if it doesn't exist yet. Costs ~0.002 SOL rent
        # which Jupiter unwraps as needed.
        "wrapAndUnwrapSol": True,
        # Compute unit price (priority fee). Locked at config level; bot
        # operator tunes via env. 0 means "let Jupiter pick a sensible
        # default" — generally fine but slower at network congestion.
        "computeUnitPriceMicroLamports": priority_fee_lamports,
        "dynamicComputeUnitLimit": True,
    }
    try:
        async with session.post(
            JUPITER_SWAP_URL, json=body,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status != 200:
                log.warning("jupiter_swap_build_failed", status=r.status)
                return None
            data = await r.json()
    except Exception:
        log.exception("jupiter_swap_build_exception")
        return None

    tx_b64 = data.get("swapTransaction")
    if not tx_b64:
        log.warning("jupiter_swap_build_no_tx", body_keys=list(data.keys())[:6])
        return None
    return tx_b64


async def send_transaction(
    session: aiohttp.ClientSession,
    signed_tx_b64: str,
) -> Optional[str]:
    """Submit a signed transaction via Helius RPC. Returns the signature
    string on accept, None on rejection."""
    settings = get_copy_settings()
    rpc_url = settings.helius_rpc_url.rstrip("/")
    if settings.helius_api_key and "api-key" not in rpc_url:
        rpc_url = f"{rpc_url}/?api-key={settings.helius_api_key}"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [
            signed_tx_b64,
            {
                "encoding": "base64",
                # We pre-checked the sim on Jupiter's side — skip the RPC
                # preflight to save latency and avoid double-simulation
                # discrepancies when memecoin price moves between sim+send.
                "skipPreflight": True,
                "maxRetries": 2,
            },
        ],
    }
    try:
        async with session.post(
            rpc_url, json=payload,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            if r.status != 200:
                log.warning("rpc_send_tx_failed", status=r.status)
                return None
            data = await r.json()
    except Exception:
        log.exception("rpc_send_tx_exception")
        return None
    if "error" in data:
        log.warning("rpc_send_tx_error", error=str(data["error"])[:200])
        return None
    sig = data.get("result")
    if not isinstance(sig, str):
        return None
    return sig


async def confirm_transaction(
    session: aiohttp.ClientSession,
    signature: str,
    timeout_sec: int = 45,
    poll_interval_sec: float = 1.5,
) -> tuple[str, Optional[dict[str, Any]]]:
    """Poll getSignatureStatuses until tx confirms or timeout. Returns
    (status, meta) where status is 'confirmed'|'failed'|'dropped' and
    meta is the raw status blob from RPC."""
    settings = get_copy_settings()
    rpc_url = settings.helius_rpc_url.rstrip("/")
    if settings.helius_api_key and "api-key" not in rpc_url:
        rpc_url = f"{rpc_url}/?api-key={settings.helius_api_key}"

    deadline = asyncio.get_event_loop().time() + timeout_sec
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignatureStatuses",
        "params": [[signature], {"searchTransactionHistory": True}],
    }
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with session.post(
                rpc_url, json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    await asyncio.sleep(poll_interval_sec)
                    continue
                data = await r.json()
        except Exception:
            await asyncio.sleep(poll_interval_sec)
            continue
        statuses = (data.get("result") or {}).get("value") or [None]
        st = statuses[0]
        if st is None:
            await asyncio.sleep(poll_interval_sec)
            continue
        if st.get("err") is not None:
            return ("failed", st)
        confirmation_status = st.get("confirmationStatus")
        if confirmation_status in ("confirmed", "finalized"):
            return ("confirmed", st)
        await asyncio.sleep(poll_interval_sec)
    return ("dropped", None)


async def fetch_tx_balance_changes(
    session: aiohttp.ClientSession,
    signature: str,
) -> Optional[dict[str, int]]:
    """After confirm, fetch the full tx to compute actual in/out amounts.

    Returns {mint -> net_amount_atomic} for the bot wallet's mints. Net
    is positive for tokens we received, negative for tokens we paid.

    Memecoin swap PnL math needs this — Jupiter's quote is a prediction;
    the actual fill comes from on-chain post-balances minus pre-balances.
    """
    settings = get_copy_settings()
    rpc_url = settings.helius_rpc_url.rstrip("/")
    if settings.helius_api_key and "api-key" not in rpc_url:
        rpc_url = f"{rpc_url}/?api-key={settings.helius_api_key}"
    user_pubkey = public_key_b58()
    if user_pubkey is None:
        return None
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
        ],
    }
    try:
        async with session.post(
            rpc_url, json=payload,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except Exception:
        log.exception("rpc_get_tx_exception", signature=signature[:16])
        return None
    result = data.get("result")
    if not isinstance(result, dict):
        return None
    meta = result.get("meta") or {}
    pre_balances = {b.get("mint"): int(b.get("uiTokenAmount", {}).get("amount", 0))
                    for b in (meta.get("preTokenBalances") or [])
                    if b.get("owner") == user_pubkey}
    post_balances = {b.get("mint"): int(b.get("uiTokenAmount", {}).get("amount", 0))
                     for b in (meta.get("postTokenBalances") or [])
                     if b.get("owner") == user_pubkey}
    deltas: dict[str, int] = {}
    for mint in set(pre_balances) | set(post_balances):
        delta = post_balances.get(mint, 0) - pre_balances.get(mint, 0)
        if delta != 0:
            deltas[mint] = delta
    return deltas


async def _attempt_swap_usdc_to_token(
    session: aiohttp.ClientSession,
    *,
    output_mint: str,
    amount_in_atomic: int,
    slippage_bps: int,
    priority_fee_lamports: int,
    confirm_timeout_sec: int,
    user_pubkey: str,
) -> SwapResult:
    """One swap attempt at a fixed slippage tolerance. Caller iterates the
    ladder; this function is unaware of escalation."""
    quote = await quote_for_swap(
        session, USDC_MINT, output_mint, amount_in_atomic, slippage_bps,
    )
    if quote is None:
        return SwapResult(status="rejected", error_message="quote_unavailable")

    tx_b64 = await build_swap_transaction(
        session, quote, user_pubkey, priority_fee_lamports,
    )
    if tx_b64 is None:
        return SwapResult(status="rejected", error_message="swap_build_failed")

    signed = sign_versioned_transaction(tx_b64)
    if signed is None:
        return SwapResult(status="rejected", error_message="signing_failed")

    signature = await send_transaction(session, signed)
    if signature is None:
        return SwapResult(status="rejected", error_message="rpc_send_failed")

    status, meta = await confirm_transaction(
        session, signature, timeout_sec=confirm_timeout_sec,
    )
    if status != "confirmed":
        return SwapResult(
            status="failed" if status == "failed" else "dropped",
            signature=signature, raw_meta=meta,
            error_message=str((meta or {}).get("err"))[:200] if meta else None,
        )

    # Resolve actual fill from on-chain post-balances.
    deltas = await fetch_tx_balance_changes(session, signature)
    actual_in = -deltas.get(USDC_MINT, -amount_in_atomic) if deltas else amount_in_atomic
    actual_out = deltas.get(output_mint, quote.out_amount_atomic) if deltas else quote.out_amount_atomic
    fill_price_usd: Optional[float] = None
    slippage_bps_actual: Optional[float] = None
    if actual_out > 0 and actual_in > 0:
        in_usd = actual_in / (10 ** USDC_DECIMALS)
        fill_price_usd = in_usd / actual_out
        if quote.out_amount_atomic > 0:
            slippage_bps_actual = (
                (quote.out_amount_atomic - actual_out)
                / quote.out_amount_atomic * 10_000.0
            )
    return SwapResult(
        status="filled",
        signature=signature,
        fill_price_usd=fill_price_usd,
        actual_in_atomic=actual_in,
        actual_out_atomic=actual_out,
        slippage_bps=slippage_bps_actual,
        # Jupiter takes a 0.10% platform fee on memecoin swaps. Network
        # fees + priority fee are SOL, not USDC — tracked separately.
        fees_usd=(actual_in / (10 ** USDC_DECIMALS)) * 0.001 if actual_in > 0 else 0.0,
        raw_meta=meta,
    )


async def execute_swap_usdc_to_token(
    session: aiohttp.ClientSession,
    output_mint: str,
    notional_usd: float,
    slippage_ladder: tuple[int, ...] | list[int],
    priority_fee_lamports: int,
    confirm_timeout_sec: int = 45,
) -> SwapResult:
    """Top-level entry-side swap with adaptive slippage escalation.

    Tries each tier of `slippage_ladder` in order. Stops at the first
    `filled` result; otherwise escalates per `should_escalate()`. Returns
    the last attempt's result if the ladder is exhausted, with
    `attempt_index` and `slippage_bps_used` populated for telemetry.

    Per brainstorm 2026-05-30 spec: default ladder is [200, 500, 1500,
    3000] bps. The tight 200 tier should fill liquid tokens at near-mid
    price; loose 3000 covers low-liquidity memecoins.
    """
    if not is_wallet_available():
        return SwapResult(status="no_wallet")
    user_pubkey = public_key_b58()
    if user_pubkey is None:
        return SwapResult(status="no_wallet")

    amount_in_atomic = int(notional_usd * (10 ** USDC_DECIMALS))
    if amount_in_atomic <= 0:
        return SwapResult(status="rejected", error_message="zero_input")

    if not slippage_ladder:
        return SwapResult(status="rejected", error_message="empty_slippage_ladder")

    last_result = SwapResult(status="rejected", error_message="ladder_not_executed")
    for i, slippage_bps in enumerate(slippage_ladder):
        log.info(
            "swap_attempt", direction="buy",
            attempt=i, slippage_bps=slippage_bps,
            output_mint=output_mint, notional_usd=notional_usd,
        )
        result = await _attempt_swap_usdc_to_token(
            session,
            output_mint=output_mint,
            amount_in_atomic=amount_in_atomic,
            slippage_bps=slippage_bps,
            priority_fee_lamports=priority_fee_lamports,
            confirm_timeout_sec=confirm_timeout_sec,
            user_pubkey=user_pubkey,
        )
        result.attempt_index = i
        result.slippage_bps_used = slippage_bps
        if not should_escalate(result):
            return result
        log.info(
            "swap_escalating", direction="buy",
            attempt=i, attempted_bps=slippage_bps,
            status=result.status, error=result.error_message,
        )
        last_result = result
    return last_result


async def _attempt_swap_token_to_usdc(
    session: aiohttp.ClientSession,
    *,
    input_mint: str,
    amount_in_atomic: int,
    slippage_bps: int,
    priority_fee_lamports: int,
    confirm_timeout_sec: int,
    user_pubkey: str,
) -> SwapResult:
    """One exit-side swap attempt at a fixed slippage tolerance."""
    quote = await quote_for_swap(
        session, input_mint, USDC_MINT, amount_in_atomic, slippage_bps,
    )
    if quote is None:
        return SwapResult(status="rejected", error_message="quote_unavailable")

    tx_b64 = await build_swap_transaction(
        session, quote, user_pubkey, priority_fee_lamports,
    )
    if tx_b64 is None:
        return SwapResult(status="rejected", error_message="swap_build_failed")

    signed = sign_versioned_transaction(tx_b64)
    if signed is None:
        return SwapResult(status="rejected", error_message="signing_failed")

    signature = await send_transaction(session, signed)
    if signature is None:
        return SwapResult(status="rejected", error_message="rpc_send_failed")

    status, meta = await confirm_transaction(
        session, signature, timeout_sec=confirm_timeout_sec,
    )
    if status != "confirmed":
        return SwapResult(
            status="failed" if status == "failed" else "dropped",
            signature=signature, raw_meta=meta,
            error_message=str((meta or {}).get("err"))[:200] if meta else None,
        )

    deltas = await fetch_tx_balance_changes(session, signature)
    actual_in = -deltas.get(input_mint, -amount_in_atomic) if deltas else amount_in_atomic
    actual_out = deltas.get(USDC_MINT, quote.out_amount_atomic) if deltas else quote.out_amount_atomic
    fill_price_usd: Optional[float] = None
    if actual_out > 0 and actual_in > 0:
        out_usd = actual_out / (10 ** USDC_DECIMALS)
        fill_price_usd = out_usd / actual_in
    slippage_bps_actual: Optional[float] = None
    if quote.out_amount_atomic > 0:
        slippage_bps_actual = (
            (quote.out_amount_atomic - actual_out)
            / quote.out_amount_atomic * 10_000.0
        )
    return SwapResult(
        status="filled",
        signature=signature,
        fill_price_usd=fill_price_usd,
        actual_in_atomic=actual_in,
        actual_out_atomic=actual_out,
        slippage_bps=slippage_bps_actual,
        fees_usd=(actual_out / (10 ** USDC_DECIMALS)) * 0.001 if actual_out > 0 else 0.0,
        raw_meta=meta,
    )


async def execute_swap_token_to_usdc(
    session: aiohttp.ClientSession,
    input_mint: str,
    amount_in_atomic: int,
    slippage_ladder: tuple[int, ...] | list[int],
    priority_fee_lamports: int,
    confirm_timeout_sec: int = 45,
) -> SwapResult:
    """Exit-side swap with adaptive slippage escalation. Same ladder logic
    as the entry side. `amount_in_atomic` is the full position size being
    closed (in the input token's atomic units)."""
    if not is_wallet_available():
        return SwapResult(status="no_wallet")
    user_pubkey = public_key_b58()
    if user_pubkey is None:
        return SwapResult(status="no_wallet")
    if amount_in_atomic <= 0:
        return SwapResult(status="rejected", error_message="zero_input")
    if not slippage_ladder:
        return SwapResult(status="rejected", error_message="empty_slippage_ladder")

    last_result = SwapResult(status="rejected", error_message="ladder_not_executed")
    for i, slippage_bps in enumerate(slippage_ladder):
        log.info(
            "swap_attempt", direction="sell",
            attempt=i, slippage_bps=slippage_bps,
            input_mint=input_mint, amount_in_atomic=amount_in_atomic,
        )
        result = await _attempt_swap_token_to_usdc(
            session,
            input_mint=input_mint,
            amount_in_atomic=amount_in_atomic,
            slippage_bps=slippage_bps,
            priority_fee_lamports=priority_fee_lamports,
            confirm_timeout_sec=confirm_timeout_sec,
            user_pubkey=user_pubkey,
        )
        result.attempt_index = i
        result.slippage_bps_used = slippage_bps
        if not should_escalate(result):
            return result
        log.info(
            "swap_escalating", direction="sell",
            attempt=i, attempted_bps=slippage_bps,
            status=result.status, error=result.error_message,
        )
        last_result = result
    return last_result
