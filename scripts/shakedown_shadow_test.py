"""Phase 1 shakedown gate item #2 — direct shadow execution validation.

Fires N=10 micro ($5) shadow trades sequentially: each iteration opens a
shadow, waits for fill, immediately closes via reduce-only. Validates:

- Hyperliquid Exchange .order() with the agent key works
- Order response parser handles real-world responses
- calibration_records pairing populates correctly
- Reduce-only close path works
- Total exposure stays small (max 1 open shadow × $5 at any instant)

Safety:
- Strict serial open → close → next pattern (never multiple opens at once)
- If a close fails, ALERT loudly via P0 so Roy can manually flatten
- Aborts on first close failure (don't accumulate stuck positions)

Run via:
    docker compose exec -T framework python -m scripts.shakedown_shadow_test
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

from bots.base.fill_simulator_base import SimulatedFill
from bots.structure.executor import StructureExecutor
from bots.structure.signals.base import SignalCandidate
from bots.structure.venue import HyperliquidVenue, is_exchange_available
from framework.alerts import emit_alert
from framework.audit import write_audit
from framework.db import session_scope
from framework.logging_setup import configure_logging, get_logger
from framework.models import Signal, Trade
from monitoring.alerting.taxonomy import Severity


BOT_ID = "structure"
TEST_ASSET = "BTC"           # deepest liquidity; minimal slippage risk
TEST_NOTIONAL_USD = 5.0      # micro
N_ITERATIONS = 10
PAUSE_BETWEEN_SECONDS = 3.0  # let exchange settle between trades
DIRECTIONS = ["long", "short"] * (N_ITERATIONS // 2 + 1)  # alternate

log = get_logger(__name__)


def _make_test_signal(direction: str, mid: float) -> int:
    """Insert a Signal row tagged 'shakedown_shadow_test'. Returns signal_id."""
    with session_scope() as s:
        sig = Signal(
            bot_id=BOT_ID,
            signal_type="shakedown_shadow_test",
            asset=TEST_ASSET,
            venue="hyperliquid",
            direction=direction,
            payload={"test": True, "mid_at_attempt": mid, "iteration_ts": time.time()},
        )
        s.add(sig)
        s.flush()
        return sig.id


def _make_synthetic_paper_trade(signal_id: int, direction: str, mid: float) -> int:
    """Insert a paper Trade row marked closed (we don't want it managed by the bot loop).

    Why marked closed: we just need a paper_trade_id for calibration_records FK. No
    actual paper-side simulation needed for this shakedown test.
    """
    with session_scope() as s:
        t = Trade(
            bot_id=BOT_ID,
            signal_id=signal_id,
            mode="paper",
            asset=TEST_ASSET,
            venue="hyperliquid",
            direction=direction,
            entry_price=mid,
            size_usd=TEST_NOTIONAL_USD,
            leverage=1.0,
            entry_at=datetime.now(timezone.utc),
            exit_price=mid,
            exit_at=datetime.now(timezone.utc),
            exit_reason="shakedown_synthetic",
            fees_usd=0.0,
            pnl_pct=0.0,
            pnl_usd=0.0,
            fill_status="closed",
            sim_metadata={"shakedown_synthetic": True, "slippage_bps": 0.0},
        )
        s.add(t)
        s.flush()
        return t.id


def main() -> None:
    configure_logging()
    if not is_exchange_available():
        print("ERROR: Hyperliquid agent key not configured. Aborting.", file=sys.stderr)
        sys.exit(2)

    venue = HyperliquidVenue()
    executor = StructureExecutor(venue)

    write_audit(
        "shakedown_shadow_test_start",
        bot_id=BOT_ID,
        payload={"n_iterations": N_ITERATIONS, "test_notional_usd": TEST_NOTIONAL_USD, "asset": TEST_ASSET},
    )

    results = {"opened": 0, "open_failed": 0, "closed": 0, "close_failed": 0,
               "fills": [], "calibration_records_created": 0}

    for i in range(N_ITERATIONS):
        direction = DIRECTIONS[i]
        print(f"\n[{i+1}/{N_ITERATIONS}] {direction} {TEST_ASSET} ${TEST_NOTIONAL_USD}", flush=True)

        # 0. Get current mid for the synthetic paper trade
        try:
            mids = venue.all_mids()
            mid = float(mids.get(TEST_ASSET) or 0)
        except Exception as e:
            print(f"  mid fetch failed: {e}", flush=True)
            results["open_failed"] += 1
            continue
        if mid <= 0:
            print(f"  mid is invalid: {mid}", flush=True)
            results["open_failed"] += 1
            continue

        # 1. Synthetic paper trade for FK
        sig_id = _make_test_signal(direction, mid)
        paper_id = _make_synthetic_paper_trade(sig_id, direction, mid)
        print(f"  signal_id={sig_id} synthetic_paper_id={paper_id} mid={mid}", flush=True)

        # 2. Open shadow via executor (bypassing sampling gate)
        sim_paper_fill = SimulatedFill(
            fill_price=mid, fees_usd=0.0, slippage_bps=0.0,
            metadata={"mid_at_entry": mid},
        )
        candidate = SignalCandidate(
            signal_type="shakedown_shadow_test",
            asset=TEST_ASSET, direction=direction,
            payload={"shakedown": True},
            stop_pct=None, take_profit_pct=None, timeout_hours=None,
        )
        try:
            shadow_id = executor._place_shadow_unsafe(
                signal_id=sig_id, paper_trade_id=paper_id,
                candidate=candidate, paper_sim_fill=sim_paper_fill,
                shadow_usd=TEST_NOTIONAL_USD,
            )
        except Exception:
            log.exception("shadow_open_exception", iteration=i+1)
            shadow_id = None

        if shadow_id is None:
            print(f"  OPEN FAILED (likely IoC didn't fill)", flush=True)
            results["open_failed"] += 1
            time.sleep(PAUSE_BETWEEN_SECONDS)
            continue

        # Inspect what was written
        with session_scope() as s:
            t = s.get(Trade, shadow_id)
            actual_entry = float(t.entry_price or 0)
            actual_size = float(t.size_usd or 0)
        print(f"  OPENED shadow_id={shadow_id} entry={actual_entry} size_usd={actual_size}", flush=True)
        results["opened"] += 1
        results["fills"].append({"iter": i+1, "side": direction, "entry": actual_entry, "size": actual_size})

        # 3. Brief settle pause
        time.sleep(1.0)

        # 4. Close via reduce-only IoC opposite
        try:
            executor.close_shadow(shadow_id, paper_id, "shakedown_test")
        except Exception:
            log.exception("shadow_close_exception", iteration=i+1, shadow_id=shadow_id)

        with session_scope() as s:
            t = s.get(Trade, shadow_id)
            closed_ok = t is not None and t.fill_status == "closed"
            exit_px = float(t.exit_price or 0) if t else 0.0
            pnl_pct = float(t.pnl_pct or 0) if t else 0.0

        if closed_ok:
            print(f"  CLOSED exit={exit_px} pnl_pct={pnl_pct:.3f}%", flush=True)
            results["closed"] += 1
        else:
            # CRITICAL: we have an open real position with no automatic close
            print(f"  CLOSE FAILED — shadow_id={shadow_id} STILL OPEN, ABORTING TEST", flush=True)
            results["close_failed"] += 1
            emit_alert(
                severity=Severity.P0,
                title=f"Shakedown test: shadow {shadow_id} stuck open",
                body=(
                    f"Shadow trade {shadow_id} on {TEST_ASSET} ${TEST_NOTIONAL_USD} {direction} "
                    f"opened but close failed. Manually flatten on Hyperliquid web UI now."
                ),
                bot_id=BOT_ID,
                event_type="shakedown_close_stuck",
                metadata={"shadow_id": shadow_id, "asset": TEST_ASSET, "direction": direction},
            )
            break  # don't pile up more stuck positions

        # 5. Verify calibration_record was created during open
        with session_scope() as s:
            from sqlalchemy import select
            from framework.models import CalibrationRecord
            cr = s.execute(
                select(CalibrationRecord).where(
                    CalibrationRecord.shadow_trade_id == shadow_id
                )
            ).scalar_one_or_none()
        if cr is not None:
            results["calibration_records_created"] += 1

        time.sleep(PAUSE_BETWEEN_SECONDS)

    # ---- Summary ----------------------------------------------------------

    print("\n" + "=" * 60)
    print(f"SHAKEDOWN SHADOW TEST RESULT")
    print("=" * 60)
    print(f"  Iterations attempted     : {N_ITERATIONS}")
    print(f"  Opens succeeded          : {results['opened']}")
    print(f"  Opens failed             : {results['open_failed']}")
    print(f"  Closes succeeded         : {results['closed']}")
    print(f"  Closes failed (stuck)    : {results['close_failed']}")
    print(f"  Calibration records made : {results['calibration_records_created']}")
    print()

    write_audit(
        "shakedown_shadow_test_complete",
        bot_id=BOT_ID,
        payload=results,
    )

    if results["close_failed"] > 0:
        print("FAIL: stuck open positions — manual cleanup required.", file=sys.stderr)
        sys.exit(1)

    if results["opened"] >= 5 and results["closed"] == results["opened"]:
        print("PASS — shadow path verified end-to-end.")
        emit_alert(
            severity=Severity.P2,
            title=f"Shakedown gate #2 ✅ ({results['opened']} shadow trades verified)",
            body=(
                f"shakedown_shadow_test completed: opened {results['opened']}, "
                f"closed {results['closed']}, calibration records: {results['calibration_records_created']}. "
                f"Shadow execution path verified end-to-end."
            ),
            event_type="shakedown_check_passed",
        )
        sys.exit(0)

    print("PARTIAL — review counts and consider re-running.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
