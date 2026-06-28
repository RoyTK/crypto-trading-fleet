"""COPY position reconciliation.

Compares bot-tracked open shadow/live positions against actual on-chain
balances in our Phantom (Solana) and Rabby (EVM) wallets.

Build A: paper-only — no shadow/live positions exist, so the fetcher will
return zero snapshots and reconciliation is a no-op.

Build B: shadow execution adds real DEX swaps. The fetcher then queries
Phantom token balances (via Helius getTokenAccountsByOwner) and Rabby
balances (via web3.py Contract.balanceOf) and compares against bot-tracked
size_usd. Drift > 0.5% → halt.
"""
from __future__ import annotations

from sqlalchemy import select

from framework.db import session_scope
from framework.logging_setup import get_logger
from framework.models import Trade
from framework.reconciliation import PositionSnapshot

log = get_logger(__name__)

BOT_ID = "copy"
EPS = 1e-9


def _bot_open_positions() -> dict[tuple[str, str], float]:
    """Sum open LIVE Trades by (venue, asset). Paper excluded — no on-chain
    counterpart. SHADOW also excluded (2026-06-28): shadow DOES hold real
    tiny on-chain positions (real Jupiter swaps, tx_signature in sim_metadata),
    BUT `_fetch_onchain_positions_solana()` is still a Build-A STUB returning {} —
    so every open shadow position reads bot_size>0 vs venue_size=0 = 100% drift
    and false-halts COPY (incident: halt 64 on DuBrjnHaC, shadow trade 930). With
    an unimplemented fetcher the check can only false-halt, never detect real drift,
    so reconciling shadow is all-cost/no-benefit. Stuck shadow trades are already
    backstopped by stale_position_cleanup (force-close at 2x timeout).
    TODO(reconciliation Build B): implement _fetch_onchain_positions_solana/_evm
    (Helius getTokenAccountsByOwner -> USD; Rabby balanceOf) and re-add 'shadow'
    here — REQUIRED before enabling live-full trading, which needs real drift
    protection."""
    out: dict[tuple[str, str], float] = {}
    with session_scope() as s:
        q = select(Trade).where(
            Trade.bot_id == BOT_ID,
            Trade.fill_status == "open",
            Trade.mode.in_(("live",)),
        )
        for trade in s.execute(q).scalars():
            if trade.size_usd is None:
                continue
            sign = 1.0 if trade.direction == "long" else -1.0
            key = (trade.venue, trade.asset)
            out[key] = out.get(key, 0.0) + (trade.size_usd * sign)
    return out


def _fetch_onchain_positions_solana() -> dict[tuple[str, str], float]:
    """Build B will query Helius getTokenAccountsByOwner for our Phantom wallet.
    Build A: returns empty (no live positions to reconcile)."""
    return {}


def _fetch_onchain_positions_evm() -> dict[tuple[str, str], float]:
    """Build B will query Rabby's balances via web3.py Contract.balanceOf.
    Build A: returns empty."""
    return {}


def make_fetcher_solana():
    """Closure for register_venue_fetcher('solana', fn)."""

    def fetcher() -> list[PositionSnapshot]:
        bot = {k: v for k, v in _bot_open_positions().items() if k[0] == "solana"}
        actual = _fetch_onchain_positions_solana()
        return _diff(bot, actual)

    return fetcher


def make_fetcher_evm(chain: str):
    """Closure for register_venue_fetcher('base'|'arbitrum', fn)."""

    def fetcher() -> list[PositionSnapshot]:
        bot = {k: v for k, v in _bot_open_positions().items() if k[0] == chain}
        actual = _fetch_onchain_positions_evm()
        actual = {k: v for k, v in actual.items() if k[0] == chain}
        return _diff(bot, actual)

    return fetcher


def _diff(
    bot: dict[tuple[str, str], float],
    actual: dict[tuple[str, str], float],
) -> list[PositionSnapshot]:
    keys = set(bot.keys()) | set(actual.keys())
    snapshots: list[PositionSnapshot] = []
    for (vn, asset) in keys:
        b = bot.get((vn, asset), 0.0)
        a = actual.get((vn, asset), 0.0)
        if abs(b) < EPS and abs(a) < EPS:
            continue
        denom = max(abs(a), abs(b), EPS)
        drift_pct = abs(b - a) / denom * 100.0
        snapshots.append(PositionSnapshot(
            bot_id=BOT_ID,
            asset=asset,
            venue=vn,
            bot_size=b,
            venue_size=a,
            drift_pct=drift_pct,
        ))
    return snapshots
