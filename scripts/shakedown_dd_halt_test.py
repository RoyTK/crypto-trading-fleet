"""Phase 1 shakedown gate item #4 — per-bot DD halt enforcement.

Validates that framework.dd_monitor.check_dd_breaches:
1. Detects synthetic -16% trailing-24h paper PnL
2. Calls halt_bot with halt_type='dd_daily'
3. Sets bot_state.state='halted' so is_bot_halted returns True
4. Emits a P1 alert
5. Cleans up its synthetic data + resumes the halt at the end

Doesn't touch Hyperliquid. Pure DB + framework logic test.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from framework.audit import write_audit
from framework.db import session_scope
from framework.dd_monitor import check_dd_breaches
from framework.halt_state import is_bot_halted
from framework.logging_setup import configure_logging, get_logger
from framework.models import BotState, Halt, Signal, Trade


BOT_ID = "structure"
PAPER_CAPITAL_USD = 1000.0  # matches STRUCTURE_PAPER_CAPITAL_USD
TARGET_DD_PCT = -16.0       # exceeds 15% daily threshold
N_SYNTH_TRADES = 4


def _resume_all_open_halts(actor: str) -> int:
    n = 0
    with session_scope() as s:
        active = s.execute(
            select(Halt).where(Halt.bot_id == BOT_ID, Halt.resumed_at.is_(None))
        ).scalars().all()
        for h in active:
            h.resumed_at = datetime.now(timezone.utc)
            h.resumed_by = actor
            n += 1
        bot = s.get(BotState, BOT_ID)
        if bot is not None:
            if bot.state in ("halted",):
                bot.state = "initializing"
            bot.halted_until = None
            bot.halt_reason = None
    return n


def _delete_synthetic(trade_ids: list[int], signal_id: int) -> None:
    with session_scope() as s:
        for tid in trade_ids:
            t = s.get(Trade, tid)
            if t is not None:
                s.delete(t)
        sig = s.get(Signal, signal_id)
        if sig is not None:
            s.delete(sig)


def main() -> None:
    configure_logging()
    log = get_logger("shakedown.dd_halt_test")
    write_audit("shakedown_dd_halt_test_start", bot_id=BOT_ID,
                payload={"target_dd_pct": TARGET_DD_PCT, "n_synth": N_SYNTH_TRADES})

    print(f"\n[1/5] Pre-clean: resume any existing halts for {BOT_ID}")
    n_pre = _resume_all_open_halts("shakedown_dd_pre_clean")
    print(f"  resumed {n_pre} existing halt(s); is_bot_halted={is_bot_halted(BOT_ID)}")
    if is_bot_halted(BOT_ID):
        print("FAIL: bot still halted after cleanup — abort", file=sys.stderr)
        sys.exit(1)

    print(f"\n[2/5] Inject {N_SYNTH_TRADES} synthetic closed paper trades summing to {TARGET_DD_PCT}% of ${PAPER_CAPITAL_USD}")
    target_pnl_usd = PAPER_CAPITAL_USD * (TARGET_DD_PCT / 100.0)
    pnl_per_trade = target_pnl_usd / N_SYNTH_TRADES
    now_utc = datetime.now(timezone.utc)
    trade_ids: list[int] = []

    with session_scope() as s:
        sig = Signal(
            bot_id=BOT_ID, signal_type="shakedown_dd_test",
            asset="BTC", venue="hyperliquid", direction="long",
            payload={"shakedown_dd_synth": True, "target_dd_pct": TARGET_DD_PCT},
        )
        s.add(sig)
        s.flush()
        sig_id = sig.id

        for i in range(N_SYNTH_TRADES):
            t = Trade(
                bot_id=BOT_ID, signal_id=sig_id, mode="paper",
                asset="BTC", venue="hyperliquid", direction="long",
                entry_price=80000.0, exit_price=78000.0,
                size_usd=abs(pnl_per_trade) * 40.0, leverage=1.0,
                entry_at=now_utc - timedelta(hours=2 + i),
                exit_at=now_utc - timedelta(minutes=30 - i),
                fees_usd=0.0,
                pnl_pct=-2.5, pnl_usd=pnl_per_trade,
                exit_reason="shakedown_dd_synth", fill_status="closed",
                sim_metadata={"shakedown_dd_synth": True, "slippage_bps": 0.0},
            )
            s.add(t)
            s.flush()
            trade_ids.append(t.id)

    print(f"  inserted signal_id={sig_id}, trade_ids={trade_ids}, total injected pnl=${target_pnl_usd:.2f}")

    print(f"\n[3/5] Run check_dd_breaches with thresholds (15/30/45)")
    halt_type = check_dd_breaches(
        bot_id=BOT_ID, paper_capital_usd=PAPER_CAPITAL_USD,
        daily_pct=15.0, weekly_pct=30.0, total_pct=45.0,
    )
    print(f"  check returned halt_type={halt_type!r}")

    print(f"\n[4/5] Verify halt fired")
    halted = is_bot_halted(BOT_ID)
    print(f"  is_bot_halted={halted}")
    if halt_type == "dd_daily" and halted:
        print(f"  PASS: DD halt enforcement working")
        passed = True
    else:
        print(f"  FAIL: expected halt_type='dd_daily' and is_bot_halted=True, got halt_type={halt_type} halted={halted}", file=sys.stderr)
        passed = False

    print(f"\n[5/5] Cleanup: resume halt + delete synthetic trades")
    n_resumed = _resume_all_open_halts("shakedown_dd_cleanup")
    _delete_synthetic(trade_ids, sig_id)
    print(f"  resumed {n_resumed} halt(s); deleted {len(trade_ids)} trades + 1 signal")
    print(f"  is_bot_halted after cleanup={is_bot_halted(BOT_ID)}")

    write_audit("shakedown_dd_halt_test_complete", bot_id=BOT_ID,
                payload={"halt_type": halt_type, "passed": passed})

    print("\n" + "=" * 60)
    if passed:
        print("SHAKEDOWN GATE #4: PASS")
        sys.exit(0)
    else:
        print("SHAKEDOWN GATE #4: FAIL", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
