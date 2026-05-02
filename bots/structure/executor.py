"""STRUCTURE executor — paper + shadow trade placement.

Two modes:

- PAPER: writes a Trade row using the simulator's fill price. Always called.
- SHADOW: places a small ($5-20) real order via the Hyperliquid Exchange,
         writes a Trade row mode='shadow' with the actual fill, and pairs into
         calibration_records with the matched paper trade. Sampled at
         STRUCTURE_SHADOW_PCT (default 10%) of paper signals, AND only when
         the agent key is configured AND when the open-shadow notional cap is
         not already breached.

Calibration: each paper trade with a shadow gets a row in calibration_records
that we can later compute calibration_ratio on (actual_pnl_pct / sim_pnl_pct).

Safety:
- Hard cap: open shadow notional + new shadow <= SHADOW_OPEN_CAP_USD ($40 of $50).
  If breached, skip shadow placement (paper still happens).
- IoC limit orders: market-equivalent but with explicit slippage tolerance.
  No limit-on-book exposure if the order doesn't fill.
- Reduce-only on shadow exits: never accidentally flips a position.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select

from bots.base.executor_base import Executor
from bots.base.fill_simulator_base import SimulatedFill
from bots.structure.config import TAKER_FEE_PCT, get_structure_settings
from bots.structure.signals.base import SignalCandidate
from bots.structure.venue import HyperliquidVenue, is_exchange_available
from framework.audit import write_audit
from framework.db import session_scope
from framework.logging_setup import get_logger
from framework.models import CalibrationRecord, Trade


log = get_logger(__name__)
BOT_ID = "structure"

# Per-shadow notional band (locked Item #1 design)
SHADOW_NOTIONAL_MIN_USD = 5.0
SHADOW_NOTIONAL_MAX_USD = 20.0

# Total open-shadow exposure cap (out of ~$50 HL equity)
SHADOW_OPEN_CAP_USD = 40.0

# Slippage tolerance for IoC limit orders. We're trading $5-20; even a 2%
# adverse move is $0.10-0.40 of cost. Wider tolerance reduces no-fill rate.
SHADOW_SLIPPAGE_TOLERANCE = 0.02  # 2%

# Hyperliquid uses limit IoC for market-equivalent. Time-in-force payload.
IOC_ORDER_TYPE = {"limit": {"tif": "Ioc"}}


def _shadow_notional_for(paper_notional_usd: float) -> float:
    """Map paper notional -> shadow notional. Paper $5000 -> shadow $5,
    paper $20000 -> shadow $20, etc. Bound to [$5, $20]."""
    raw = paper_notional_usd * 0.001
    return max(SHADOW_NOTIONAL_MIN_USD, min(SHADOW_NOTIONAL_MAX_USD, raw))


def _open_shadow_notional() -> float:
    """Sum of size_usd for currently-open shadow trades."""
    with session_scope() as s:
        q = select(Trade).where(
            Trade.bot_id == BOT_ID,
            Trade.mode == "shadow",
            Trade.fill_status == "open",
        )
        return sum(float(t.size_usd or 0.0) for t in s.execute(q).scalars())


class StructureExecutor(Executor):
    """Executor for STRUCTURE bot. Holds a HyperliquidVenue for shadow orders."""

    def __init__(self, venue: HyperliquidVenue) -> None:
        self.venue = venue
        self.settings = get_structure_settings()

    # ------------------------------------------------------------------
    # Paper
    # ------------------------------------------------------------------

    def place_paper(
        self,
        signal_id: int,
        candidate: SignalCandidate,
        sim_fill: SimulatedFill,
        notional_usd: float,
        leverage: float,
    ) -> Optional[int]:
        """Insert a paper Trade row from sim_fill. Returns trade_id (or None
        on internal error). For no-fill simulator results, writes a row with
        fill_status='no_fill'."""
        with session_scope() as s:
            trade = Trade(
                bot_id=BOT_ID,
                signal_id=signal_id,
                mode="paper",
                asset=candidate.asset,
                venue=candidate.venue,
                direction=candidate.direction,
                entry_price=sim_fill.fill_price,
                size_usd=notional_usd if sim_fill.fill_price is not None else None,
                leverage=leverage,
                entry_at=datetime.now(timezone.utc) if sim_fill.fill_price is not None else None,
                fees_usd=sim_fill.fees_usd,
                fill_status="open" if sim_fill.fill_price is not None else "no_fill",
                sim_metadata={
                    "slippage_bps": sim_fill.slippage_bps,
                    "no_fill_reason": sim_fill.no_fill_reason,
                    "fill_metadata": sim_fill.metadata,
                    "stop_pct": candidate.stop_pct,
                    "take_profit_pct": candidate.take_profit_pct,
                    "timeout_hours": candidate.timeout_hours,
                },
            )
            s.add(trade)
            s.flush()
            trade_id = trade.id
        write_audit(
            "paper_trade_opened" if sim_fill.fill_price is not None else "paper_trade_no_fill",
            bot_id=BOT_ID,
            payload={
                "trade_id": trade_id, "signal_id": signal_id,
                "asset": candidate.asset, "direction": candidate.direction,
                "size_usd": notional_usd, "leverage": leverage,
            },
        )
        return trade_id

    # ------------------------------------------------------------------
    # Shadow
    # ------------------------------------------------------------------

    def maybe_place_shadow(
        self,
        signal_id: int,
        paper_trade_id: int,
        candidate: SignalCandidate,
        paper_sim_fill: SimulatedFill,
        paper_notional_usd: float,
    ) -> Optional[int]:
        """Probabilistically place a real shadow order paired to a paper trade.

        Returns the shadow trade_id, or None if the shadow was skipped (sampling,
        no key, exposure cap, or no-fill on the venue). Never raises — failures
        are logged and swallowed because shadow execution must NEVER block the
        paper-trade path.
        """
        # 1. Sampling
        pct = self.settings.structure_shadow_pct
        if pct <= 0 or random.random() * 100.0 >= pct:
            return None

        # 2. Agent key configured?
        if not is_exchange_available():
            log.debug("shadow_skipped_no_agent_key")
            return None

        # 3. Size + cap
        shadow_usd = _shadow_notional_for(paper_notional_usd)
        try:
            current_open = _open_shadow_notional()
        except Exception:
            log.exception("shadow_skipped_open_query_failed")
            return None
        if current_open + shadow_usd > SHADOW_OPEN_CAP_USD:
            log.warning(
                "shadow_skipped_exposure_cap",
                open_notional=current_open,
                attempted=shadow_usd,
                cap=SHADOW_OPEN_CAP_USD,
            )
            return None

        # 4. Place the order
        try:
            return self._place_shadow_unsafe(
                signal_id=signal_id,
                paper_trade_id=paper_trade_id,
                candidate=candidate,
                paper_sim_fill=paper_sim_fill,
                shadow_usd=shadow_usd,
            )
        except Exception:
            log.exception("shadow_place_failed", asset=candidate.asset)
            return None

    def _place_shadow_unsafe(
        self,
        *,
        signal_id: int,
        paper_trade_id: int,
        candidate: SignalCandidate,
        paper_sim_fill: SimulatedFill,
        shadow_usd: float,
    ) -> Optional[int]:
        """Inner logic — assumes pre-flight checks have passed. Caller wraps in
        try/except.
        """
        # Reference price from the simulator's own snapshot for sizing
        sim_meta = paper_sim_fill.metadata or {}
        ref_price = sim_meta.get("mid_at_entry") or paper_sim_fill.fill_price
        if not ref_price or ref_price <= 0:
            log.warning("shadow_skipped_no_ref_price", asset=candidate.asset)
            return None

        # Size in native units (asset-specific lot rounding handled by SDK)
        sz_native = shadow_usd / ref_price
        if sz_native <= 0:
            return None

        is_buy = (candidate.direction == "long")
        # Aggressive limit price within slippage tolerance — IoC means it
        # fills now or it dies, so we won't leave a resting order on book.
        limit_px = ref_price * (1.0 + SHADOW_SLIPPAGE_TOLERANCE) if is_buy \
            else ref_price * (1.0 - SHADOW_SLIPPAGE_TOLERANCE)

        log.info(
            "shadow_place_attempt",
            asset=candidate.asset, is_buy=is_buy,
            sz=sz_native, limit_px=limit_px, notional_usd=shadow_usd,
        )

        # Round limit_px to a sane number of digits to avoid SDK rejection on
        # asset-specific tick size. Conservative: 5 sig figs.
        limit_px = float(f"{limit_px:.5g}")

        result = self.venue.exchange.order(
            candidate.asset, is_buy, sz_native, limit_px, IOC_ORDER_TYPE,
            reduce_only=False,
        )
        actual_px, actual_sz, oid = self._parse_order_response(result)
        if actual_px is None:
            # Order rejected or not filled — IoC means no resting position
            log.warning("shadow_no_fill", asset=candidate.asset, response=str(result)[:300])
            write_audit(
                "shadow_no_fill",
                bot_id=BOT_ID,
                payload={"signal_id": signal_id, "paper_trade_id": paper_trade_id,
                         "asset": candidate.asset, "response_preview": str(result)[:300]},
            )
            return None

        actual_notional = actual_px * actual_sz
        actual_fees = actual_notional * (TAKER_FEE_PCT / 100.0)

        # Insert shadow Trade row + calibration_records pair
        with session_scope() as s:
            trade = Trade(
                bot_id=BOT_ID,
                signal_id=signal_id,
                mode="shadow",
                asset=candidate.asset,
                venue=candidate.venue,
                direction=candidate.direction,
                entry_price=actual_px,
                size_usd=actual_notional,
                leverage=1.0,  # shadow is spot-equivalent at micro size
                entry_at=datetime.now(timezone.utc),
                fees_usd=actual_fees,
                fill_status="open",
                sim_metadata={
                    "oid": oid,
                    "ref_price_at_attempt": ref_price,
                    "limit_px": limit_px,
                    "slippage_tolerance": SHADOW_SLIPPAGE_TOLERANCE,
                    "stop_pct": candidate.stop_pct,
                    "take_profit_pct": candidate.take_profit_pct,
                    "timeout_hours": candidate.timeout_hours,
                },
            )
            s.add(trade)
            s.flush()
            shadow_trade_id = trade.id

            calib = CalibrationRecord(
                bot_id=BOT_ID,
                signal_id=signal_id,
                paper_trade_id=paper_trade_id,
                shadow_trade_id=shadow_trade_id,
                sim_entry_price=paper_sim_fill.fill_price,
                actual_entry_price=actual_px,
            )
            s.add(calib)

        write_audit(
            "shadow_trade_opened",
            bot_id=BOT_ID,
            payload={
                "shadow_trade_id": shadow_trade_id,
                "paper_trade_id": paper_trade_id,
                "signal_id": signal_id,
                "asset": candidate.asset,
                "direction": candidate.direction,
                "actual_entry_price": actual_px,
                "shadow_notional_usd": actual_notional,
                "oid": oid,
            },
        )
        return shadow_trade_id

    # ------------------------------------------------------------------
    # Shadow exit
    # ------------------------------------------------------------------

    def close_shadow(
        self,
        shadow_trade_id: int,
        paper_trade_id: int,
        exit_reason: str,
    ) -> None:
        """Close an open shadow trade by placing a reduce-only IoC opposite-direction
        order. Updates the Trade row and the paired calibration_record. Failures
        are logged but never raised — the paper close has already happened.
        """
        try:
            self._close_shadow_unsafe(shadow_trade_id, paper_trade_id, exit_reason)
        except Exception:
            log.exception("shadow_close_failed", shadow_trade_id=shadow_trade_id)

    def _close_shadow_unsafe(
        self,
        shadow_trade_id: int,
        paper_trade_id: int,
        exit_reason: str,
    ) -> None:
        with session_scope() as s:
            trade = s.get(Trade, shadow_trade_id)
            if trade is None or trade.fill_status != "open":
                return
            asset = trade.asset
            direction = trade.direction
            entry_price = float(trade.entry_price or 0.0)
            sz_native = float(trade.size_usd or 0.0) / entry_price if entry_price > 0 else 0.0

        if sz_native <= 0 or not is_exchange_available():
            log.warning("shadow_close_skipped", shadow_trade_id=shadow_trade_id)
            return

        # Opposite direction, reduce_only
        is_buy = (direction == "short")  # closing a short = buy
        # Use last mid as ref price — reasonably current vs entry
        try:
            mids = self.venue.all_mids()
            ref = mids.get(asset) or entry_price
        except Exception:
            ref = entry_price
        slip = 1.0 + SHADOW_SLIPPAGE_TOLERANCE if is_buy else 1.0 - SHADOW_SLIPPAGE_TOLERANCE
        limit_px = float(f"{(ref * slip):.5g}")

        result = self.venue.exchange.order(
            asset, is_buy, sz_native, limit_px, IOC_ORDER_TYPE,
            reduce_only=True,
        )
        actual_px, actual_sz, oid = self._parse_order_response(result)
        if actual_px is None:
            log.warning("shadow_exit_no_fill", shadow_trade_id=shadow_trade_id, response=str(result)[:300])
            return

        actual_notional = actual_px * actual_sz
        exit_fees = actual_notional * (TAKER_FEE_PCT / 100.0)

        # Compute PnL
        if direction == "long":
            pnl_pct = (actual_px - entry_price) / entry_price * 100.0
        else:
            pnl_pct = (entry_price - actual_px) / entry_price * 100.0
        pnl_usd = (entry_price * sz_native) * (pnl_pct / 100.0) - exit_fees

        with session_scope() as s:
            t = s.get(Trade, shadow_trade_id)
            if t is None:
                return
            t.exit_price = actual_px
            t.exit_at = datetime.now(timezone.utc)
            t.exit_reason = exit_reason
            t.fill_status = "closed"
            t.fees_usd = float(t.fees_usd or 0.0) + exit_fees
            t.pnl_pct = pnl_pct
            t.pnl_usd = pnl_usd
            md = dict(t.sim_metadata or {})
            md["exit_oid"] = oid
            md["exit_limit_px"] = limit_px
            t.sim_metadata = md

            # Update paired calibration_record with exit prices + ratio
            calib = s.execute(
                select(CalibrationRecord).where(
                    CalibrationRecord.shadow_trade_id == shadow_trade_id,
                )
            ).scalar_one_or_none()
            paper = s.get(Trade, paper_trade_id)
            if calib is not None:
                calib.actual_exit_price = actual_px
                calib.actual_pnl_pct = pnl_pct
                if paper is not None and paper.exit_price and paper.entry_price:
                    sim_pnl_pct = (paper.exit_price - paper.entry_price) / paper.entry_price * 100.0
                    if paper.direction == "short":
                        sim_pnl_pct = -sim_pnl_pct
                    sim_pnl_pct *= float(paper.leverage or 1.0)
                    calib.sim_exit_price = paper.exit_price
                    calib.sim_pnl_pct = sim_pnl_pct
                    if abs(sim_pnl_pct) > 1e-9:
                        calib.calibration_ratio = pnl_pct / sim_pnl_pct

        write_audit(
            "shadow_trade_closed",
            bot_id=BOT_ID,
            payload={
                "shadow_trade_id": shadow_trade_id,
                "paper_trade_id": paper_trade_id,
                "exit_reason": exit_reason,
                "exit_price": actual_px,
                "pnl_pct": pnl_pct,
                "pnl_usd": pnl_usd,
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_order_response(
        self, response: Any,
    ) -> tuple[Optional[float], float, Optional[int]]:
        """Pull (avgPx, totalSz, oid) out of Hyperliquid's order response.

        Returns (None, 0, None) on rejection, no-fill, or unexpected shape.
        Hyperliquid response shape (success):
          {"status": "ok", "response": {"type": "order", "data": {"statuses":
            [{"filled": {"totalSz": "...", "avgPx": "...", "oid": ...}} OR
             {"resting": {"oid": ...}} OR
             {"error": "..."}]}}}
        """
        try:
            if not isinstance(response, dict) or response.get("status") != "ok":
                return None, 0.0, None
            statuses = response.get("response", {}).get("data", {}).get("statuses", [])
            if not statuses:
                return None, 0.0, None
            st = statuses[0]
            if "filled" in st:
                f = st["filled"]
                return float(f.get("avgPx", 0)), float(f.get("totalSz", 0)), f.get("oid")
            return None, 0.0, None
        except Exception:
            log.exception("parse_order_response_failed", response_preview=str(response)[:200])
            return None, 0.0, None

    # ------------------------------------------------------------------
    # Executor base interface (also used by tests / future symmetry)
    # ------------------------------------------------------------------

    def place_shadow(self, signal_id: int, sim_fill: SimulatedFill) -> Optional[int]:
        """Adapter for the abstract Executor base. Real shadow placement uses
        maybe_place_shadow which needs more context (paper_trade_id + candidate).
        """
        raise NotImplementedError("Use maybe_place_shadow with full context")

    def close_paper(
        self,
        trade_id: int,
        exit_price: float,
        exit_reason: str,
        sim_fill: SimulatedFill,
    ) -> None:
        """Adapter — routes to existing close_paper_trade in loop_helpers."""
        from bots.structure.loop_helpers import close_paper_trade
        close_paper_trade(
            trade_id=trade_id, exit_price=exit_price,
            exit_fill=sim_fill, exit_reason=exit_reason,
        )
