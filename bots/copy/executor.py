"""COPY executor — paper + sampled shadow + (gated) live execution.

Mirrors bots/structure/executor.py at the interface level — paper, shadow,
and live modes share the place + close lifecycle — but the venue is
fundamentally different. STRUCTURE swings HL perps; COPY swaps Solana
tokens via the Jupiter aggregator.

Behavior tiers (each gated independently in config.py):
- PAPER: always runs (no key, no risk). Writes a Trade(mode='paper') from
  the simulator's predicted fill. This is the live experiment data source.
- SHADOW: gated by `copy_live_enabled` AND wallet available AND sampling
  hit AND open-exposure cap not breached. Real swap of $10-25 USDC. Pairs
  to its parent paper trade via CalibrationRecord so we can compute the
  realized-vs-simulated ratio over time.
- LIVE: gated by `copy_live_enabled` AND `copy_live_full_enabled` AND
  wallet available. Full-size real swap. The two-flag gate is intentional
  — flipping live_enabled gets you SHADOW; you must also flip
  live_full_enabled to get LIVE. Defends against single-flag accidents.

Safety:
- Each path is wrapped in try/except so an executor failure can NEVER
  block the paper-trade write or crash the bot.
- Shadow exposure cap is checked atomically per-attempt against open
  shadow notional (sums `Trade.size_usd` for mode='shadow' fill_status='open').
- Live exposure cap is checked separately so shadow and live don't
  compete for the same headroom.
- close_* paths are reduce-only by construction (swap the held position
  back to USDC) — no possibility of accidentally flipping direction.
"""
from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp
from sqlalchemy import select

from bots.base.fill_simulator_base import SimulatedFill
from bots.copy.config import get_copy_settings
from bots.copy.signals.base import SignalCandidate
from bots.copy.venue.jupiter_swap import (
    SwapResult,
    execute_swap_token_to_usdc,
    execute_swap_usdc_to_token,
)
from bots.copy.venue.solana_wallet import is_wallet_available, public_key_b58
from framework.audit import write_audit
from framework.db import session_scope
from framework.logging_setup import get_logger
from framework.models import CalibrationRecord, Trade

log = get_logger(__name__)
BOT_ID = "copy"


def _open_notional_by_mode(mode: str) -> float:
    """Sum of size_usd of open COPY trades in the given mode."""
    with session_scope() as s:
        q = select(Trade).where(
            Trade.bot_id == BOT_ID,
            Trade.mode == mode,
            Trade.fill_status == "open",
        )
        return sum(float(t.size_usd or 0.0) for t in s.execute(q).scalars())


def _shadow_notional_for(paper_notional_usd: float) -> float:
    """Map paper notional → shadow notional, bounded by config band.

    Paper $1k → shadow $10 (1%), paper $2.5k → shadow $25, etc.
    Bound to [min, max] so we don't accidentally place a $0.50 shadow
    (fees dominate) or a $100 shadow (exposure spike).
    """
    s = get_copy_settings()
    raw = paper_notional_usd * 0.01  # 1% of paper
    return max(s.copy_shadow_notional_min_usd,
               min(s.copy_shadow_notional_max_usd, raw))


class CopyExecutor:
    """Async executor for the COPY bot."""

    def __init__(self) -> None:
        self.settings = get_copy_settings()

    # ------------------------------------------------------------------
    # Paper
    # ------------------------------------------------------------------

    def place_paper(
        self,
        *,
        signal_id: int,
        candidate: SignalCandidate,
        sim_fill: SimulatedFill,
        notional_usd: float,
        leverage: float = 1.0,
    ) -> Optional[int]:
        """Insert a paper Trade row. Delegates to loop_helpers.persist_paper_trade
        to avoid duplicating the wallet_tier classification + audit logic."""
        # Local import keeps loop_helpers <-> executor import direction one-way.
        from bots.copy.loop_helpers import persist_paper_trade
        return persist_paper_trade(
            signal_id=signal_id,
            candidate=candidate,
            sim_fill=sim_fill,
            notional_usd=notional_usd,
            leverage=leverage,
        )

    # ------------------------------------------------------------------
    # Shadow — sampled real swap paired to a paper trade
    # ------------------------------------------------------------------

    async def maybe_place_shadow(
        self,
        *,
        session: aiohttp.ClientSession,
        signal_id: int,
        paper_trade_id: int,
        candidate: SignalCandidate,
        paper_sim_fill: SimulatedFill,
        paper_notional_usd: float,
    ) -> Optional[int]:
        """Probabilistically place a real shadow swap. Returns the shadow
        Trade row id, or None on skip / failure. Never raises."""
        # Master gate
        if not self.settings.copy_live_enabled:
            return None
        # Sampling
        pct = self.settings.copy_shadow_pct
        if pct <= 0 or random.random() * 100.0 >= pct:
            return None
        # Wallet must be loadable
        if not is_wallet_available():
            log.debug("shadow_skipped_no_wallet")
            return None
        # Solana-only (the executor's a Solana swap path)
        if candidate.chain != "solana":
            log.debug("shadow_skipped_non_solana", chain=candidate.chain)
            return None
        # Long-only for shadow (no on-chain short primitive for memecoins)
        if candidate.direction != "long":
            return None
        # Size + exposure cap
        shadow_usd = _shadow_notional_for(paper_notional_usd)
        try:
            current_open = _open_notional_by_mode("shadow")
        except Exception:
            log.exception("shadow_skipped_open_query_failed")
            return None
        if current_open + shadow_usd > self.settings.copy_shadow_open_cap_usd:
            log.warning(
                "shadow_skipped_exposure_cap",
                open_notional=current_open,
                attempted=shadow_usd,
                cap=self.settings.copy_shadow_open_cap_usd,
            )
            return None

        try:
            return await self._place_shadow_unsafe(
                session=session,
                signal_id=signal_id,
                paper_trade_id=paper_trade_id,
                candidate=candidate,
                paper_sim_fill=paper_sim_fill,
                shadow_usd=shadow_usd,
            )
        except Exception:
            log.exception("shadow_place_failed", asset=candidate.asset)
            return None

    async def _place_shadow_unsafe(
        self,
        *,
        session: aiohttp.ClientSession,
        signal_id: int,
        paper_trade_id: int,
        candidate: SignalCandidate,
        paper_sim_fill: SimulatedFill,
        shadow_usd: float,
    ) -> Optional[int]:
        log.info(
            "shadow_place_attempt",
            asset=candidate.asset,
            shadow_usd=shadow_usd,
            wallet=public_key_b58(),
        )
        result = await execute_swap_usdc_to_token(
            session=session,
            output_mint=candidate.asset,
            notional_usd=shadow_usd,
            slippage_ladder=self.settings.get_slippage_ladder(),
            priority_fee_lamports=self.settings.copy_swap_priority_fee_micro_lamports,
            confirm_timeout_sec=self.settings.copy_swap_confirm_timeout_sec,
        )
        if result.status != "filled":
            log.warning(
                "shadow_swap_not_filled",
                asset=candidate.asset, status=result.status,
                error=result.error_message,
            )
            write_audit(
                "shadow_swap_not_filled",
                bot_id=BOT_ID,
                payload={
                    "signal_id": signal_id,
                    "paper_trade_id": paper_trade_id,
                    "asset": candidate.asset,
                    "status": result.status,
                    "error": result.error_message,
                    "signature": result.signature,
                },
            )
            return None

        return self._insert_real_trade(
            mode="shadow",
            signal_id=signal_id,
            paper_trade_id=paper_trade_id,
            candidate=candidate,
            paper_sim_fill=paper_sim_fill,
            notional_usd=shadow_usd,
            result=result,
        )

    # ------------------------------------------------------------------
    # Live — full-size real swap, double-gated
    # ------------------------------------------------------------------

    async def maybe_place_live(
        self,
        *,
        session: aiohttp.ClientSession,
        signal_id: int,
        paper_trade_id: int,
        candidate: SignalCandidate,
        paper_sim_fill: SimulatedFill,
        notional_usd: float,
    ) -> Optional[int]:
        """Place a full-exposure live swap. Double-gated:
        copy_live_enabled AND copy_live_full_enabled. Never raises.
        """
        if not (self.settings.copy_live_enabled and self.settings.copy_live_full_enabled):
            return None
        if not is_wallet_available():
            log.warning("live_skipped_no_wallet")
            return None
        if candidate.chain != "solana" or candidate.direction != "long":
            return None
        try:
            current_open = _open_notional_by_mode("live")
        except Exception:
            log.exception("live_skipped_open_query_failed")
            return None
        # Reuse shadow cap as a sanity ceiling until live capital sizing is
        # configured separately. Caller should re-tune when live ramps up.
        if current_open + notional_usd > self.settings.copy_shadow_open_cap_usd * 4:
            log.warning(
                "live_skipped_exposure_cap",
                open_notional=current_open,
                attempted=notional_usd,
            )
            return None

        try:
            result = await execute_swap_usdc_to_token(
                session=session,
                output_mint=candidate.asset,
                notional_usd=notional_usd,
                slippage_ladder=self.settings.get_slippage_ladder(),
                priority_fee_lamports=self.settings.copy_swap_priority_fee_micro_lamports,
                confirm_timeout_sec=self.settings.copy_swap_confirm_timeout_sec,
            )
        except Exception:
            log.exception("live_place_failed", asset=candidate.asset)
            return None

        if result.status != "filled":
            write_audit(
                "live_swap_not_filled",
                bot_id=BOT_ID,
                payload={
                    "signal_id": signal_id,
                    "paper_trade_id": paper_trade_id,
                    "asset": candidate.asset,
                    "status": result.status,
                    "error": result.error_message,
                    "signature": result.signature,
                },
            )
            return None

        return self._insert_real_trade(
            mode="live",
            signal_id=signal_id,
            paper_trade_id=paper_trade_id,
            candidate=candidate,
            paper_sim_fill=paper_sim_fill,
            notional_usd=notional_usd,
            result=result,
        )

    # ------------------------------------------------------------------
    # Shared insert path: writes a Trade row + (for shadow) a CalibrationRecord
    # ------------------------------------------------------------------

    def _insert_real_trade(
        self,
        *,
        mode: str,
        signal_id: int,
        paper_trade_id: int,
        candidate: SignalCandidate,
        paper_sim_fill: SimulatedFill,
        notional_usd: float,
        result: SwapResult,
    ) -> Optional[int]:
        # actual_in_atomic is USDC spent; actual_out_atomic is token received.
        spent_usdc = (result.actual_in_atomic or 0) / 1_000_000.0
        actual_fill = result.fill_price_usd
        sim_meta_extra = {
            "tx_signature": result.signature,
            "fill_price_usd": actual_fill,
            "actual_in_usdc": spent_usdc,
            "actual_out_atomic": result.actual_out_atomic,
            "realized_slippage_bps": result.slippage_bps,
            "swap_fees_usd": result.fees_usd,
            "shadow_paper_trade_id": paper_trade_id,
            # Telemetry for the adaptive-slippage analysis: which ladder
            # tier filled this trade, and at what tolerance.
            "fill_attempt_index": result.attempt_index,
            "fill_slippage_bps_used": result.slippage_bps_used,
        }
        with session_scope() as s:
            trade = Trade(
                bot_id=BOT_ID,
                signal_id=signal_id,
                mode=mode,
                asset=candidate.asset,
                venue=candidate.venue,
                direction=candidate.direction,
                entry_price=actual_fill,
                size_usd=spent_usdc if spent_usdc > 0 else notional_usd,
                leverage=1.0,
                entry_at=datetime.now(timezone.utc),
                fees_usd=float(result.fees_usd or 0.0),
                fill_status="open",
                sim_metadata={
                    **sim_meta_extra,
                    "stop_pct": candidate.stop_pct,
                    "take_profit_pct": candidate.take_profit_pct,
                    "timeout_hours": candidate.timeout_hours,
                },
            )
            s.add(trade)
            s.flush()
            real_trade_id = trade.id

            if mode == "shadow":
                calib = CalibrationRecord(
                    bot_id=BOT_ID,
                    signal_id=signal_id,
                    paper_trade_id=paper_trade_id,
                    shadow_trade_id=real_trade_id,
                    sim_entry_price=paper_sim_fill.fill_price,
                    actual_entry_price=actual_fill,
                )
                s.add(calib)

        write_audit(
            f"{mode}_trade_opened",
            bot_id=BOT_ID,
            payload={
                "trade_id": real_trade_id,
                "paper_trade_id": paper_trade_id,
                "signal_id": signal_id,
                "asset": candidate.asset,
                "size_usd": spent_usdc,
                "fill_price": actual_fill,
                "tx_signature": result.signature,
            },
        )
        return real_trade_id

    # ------------------------------------------------------------------
    # Close (shadow + live share the same path — both swap token → USDC)
    # ------------------------------------------------------------------

    async def close_real_trade(
        self,
        *,
        session: aiohttp.ClientSession,
        trade_id: int,
        exit_reason: str,
    ) -> None:
        """Close an open shadow/live trade by swapping the held token back
        to USDC. Updates the Trade row and, for shadow, the paired
        CalibrationRecord. Failures are logged but never raised."""
        try:
            await self._close_real_trade_unsafe(
                session=session,
                trade_id=trade_id,
                exit_reason=exit_reason,
            )
        except Exception:
            log.exception("close_real_trade_failed", trade_id=trade_id)

    async def _close_real_trade_unsafe(
        self,
        *,
        session: aiohttp.ClientSession,
        trade_id: int,
        exit_reason: str,
    ) -> None:
        with session_scope() as s:
            trade = s.get(Trade, trade_id)
            if trade is None or trade.fill_status != "open":
                return
            asset = trade.asset
            mode = trade.mode
            entry_price = float(trade.entry_price or 0.0)
            sim_meta = dict(trade.sim_metadata or {})
            actual_out_atomic = int(sim_meta.get("actual_out_atomic") or 0)
            paper_trade_id = int(sim_meta.get("shadow_paper_trade_id") or 0)

        if mode not in ("shadow", "live") or actual_out_atomic <= 0:
            log.warning(
                "close_real_trade_skipped_no_position",
                trade_id=trade_id, mode=mode, actual_out_atomic=actual_out_atomic,
            )
            return
        if not is_wallet_available():
            log.warning("close_real_trade_skipped_no_wallet", trade_id=trade_id)
            return

        result = await execute_swap_token_to_usdc(
            session=session,
            input_mint=asset,
            amount_in_atomic=actual_out_atomic,
            slippage_ladder=self.settings.get_slippage_ladder(),
            priority_fee_lamports=self.settings.copy_swap_priority_fee_micro_lamports,
            confirm_timeout_sec=self.settings.copy_swap_confirm_timeout_sec,
        )
        if result.status != "filled":
            log.warning(
                "close_real_trade_swap_failed",
                trade_id=trade_id, status=result.status, error=result.error_message,
            )
            write_audit(
                f"{mode}_close_swap_not_filled",
                bot_id=BOT_ID,
                payload={
                    "trade_id": trade_id,
                    "status": result.status,
                    "error": result.error_message,
                    "signature": result.signature,
                },
            )
            return

        received_usdc = (result.actual_out_atomic or 0) / 1_000_000.0
        # PnL math: USDC received vs USDC spent at entry. The Trade row's
        # size_usd is the entry USDC, so:
        #   pnl_usd = received_usdc - size_usd_at_entry - extra_fees
        # exit fees are baked into the SwapResult.fees_usd value already.
        with session_scope() as s:
            t = s.get(Trade, trade_id)
            if t is None:
                return
            entry_usdc = float(t.size_usd or 0.0)
            pnl_usd = received_usdc - entry_usdc
            pnl_pct = (pnl_usd / entry_usdc * 100.0) if entry_usdc > 0 else 0.0
            t.exit_price = result.fill_price_usd
            t.exit_at = datetime.now(timezone.utc)
            t.exit_reason = exit_reason
            t.fill_status = "closed"
            t.fees_usd = float(t.fees_usd or 0.0) + float(result.fees_usd or 0.0)
            t.pnl_usd = pnl_usd
            t.pnl_pct = pnl_pct
            md = dict(t.sim_metadata or {})
            md["exit_tx_signature"] = result.signature
            md["received_usdc"] = received_usdc
            md["exit_realized_slippage_bps"] = result.slippage_bps
            t.sim_metadata = md

            if mode == "shadow" and paper_trade_id:
                calib = s.execute(
                    select(CalibrationRecord).where(
                        CalibrationRecord.shadow_trade_id == trade_id,
                    )
                ).scalar_one_or_none()
                paper = s.get(Trade, paper_trade_id)
                if calib is not None:
                    calib.actual_exit_price = result.fill_price_usd
                    calib.actual_pnl_pct = pnl_pct
                    if paper is not None and paper.exit_price and paper.entry_price:
                        sim_pnl_pct = (
                            (paper.exit_price - paper.entry_price)
                            / paper.entry_price * 100.0
                        )
                        if paper.direction == "short":
                            sim_pnl_pct = -sim_pnl_pct
                        sim_pnl_pct *= float(paper.leverage or 1.0)
                        calib.sim_exit_price = paper.exit_price
                        calib.sim_pnl_pct = sim_pnl_pct
                        if abs(sim_pnl_pct) > 1e-9:
                            calib.calibration_ratio = pnl_pct / sim_pnl_pct

        write_audit(
            f"{mode}_trade_closed",
            bot_id=BOT_ID,
            payload={
                "trade_id": trade_id,
                "exit_reason": exit_reason,
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
                "tx_signature": result.signature,
            },
        )
