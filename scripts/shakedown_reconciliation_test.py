"""Phase 1 shakedown gate item #5 — position drift reconciliation.

Validates the bot's reconciliation cron detects when its tracked positions
diverge from Hyperliquid's actual user_state and halts the bot.

Sequence:
1. Pre-clean any existing structure halts
2. Insert a fake SHADOW Trade row for an asset where we have no actual HL
   position (e.g., XRP — assuming we don't currently hold one). This creates
   100% drift relative to HL actual.
3. Register the venue fetcher (same way StructureBot does on startup)
4. Call reconcile_once
5. Verify: drift snapshot returned for the fake asset AND bot is halted
6. Cleanup: resume halt + delete fake trade

Doesn't touch Hyperliquid (no real orders). Pure framework reconciliation
logic test.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from sqlalchemy import select

from bots.structure.reconciliation import make_fetcher
from bots.structure.venue import HyperliquidVenue
from framework.audit import write_audit
from framework.db import session_scope
from framework.halt_state import is_bot_halted
from framework.logging_setup import configure_logging, get_logger
from framework.models import BotState, Halt, Signal, Trade
from framework.reconciliation import reconcile_once, register_venue_fetcher


BOT_ID = "structure"
FAKE_ASSET = "XRP"   # arbitrary asset where we don't hold a real position
FAKE_SIZE_USD = 50.0


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


def _delete_synthetic(trade_id: int, signal_id: int) -> None:
    with session_scope() as s:
        t = s.get(Trade, trade_id)
        if t is not None:
            s.delete(t)
        sig = s.get(Signal, signal_id)
        if sig is not None:
            s.delete(sig)


def main() -> None:
    configure_logging()
    log = get_logger("shakedown.reconciliation_test")
    write_audit("shakedown_reconciliation_test_start", bot_id=BOT_ID,
                payload={"fake_asset": FAKE_ASSET, "fake_size_usd": FAKE_SIZE_USD})

    print(f"\n[1/6] Pre-clean: resume any existing halts for {BOT_ID}")
    n_pre = _resume_all_open_halts("shakedown_recon_pre_clean")
    print(f"  resumed {n_pre} existing halt(s); is_bot_halted={is_bot_halted(BOT_ID)}")
    if is_bot_halted(BOT_ID):
        print("FAIL: bot still halted after cleanup — abort", file=sys.stderr)
        sys.exit(1)

    print(f"\n[2/6] Inject fake SHADOW Trade for {FAKE_ASSET} ${FAKE_SIZE_USD} (HL has no actual position)")
    now_utc = datetime.now(timezone.utc)
    with session_scope() as s:
        sig = Signal(
            bot_id=BOT_ID, signal_type="shakedown_recon_test",
            asset=FAKE_ASSET, venue="hyperliquid", direction="long",
            payload={"shakedown_recon_synth": True},
        )
        s.add(sig)
        s.flush()
        sig_id = sig.id

        t = Trade(
            bot_id=BOT_ID, signal_id=sig_id, mode="shadow",
            asset=FAKE_ASSET, venue="hyperliquid", direction="long",
            entry_price=2.0, size_usd=FAKE_SIZE_USD, leverage=1.0,
            entry_at=now_utc, fees_usd=0.0, fill_status="open",
            sim_metadata={"shakedown_recon_synth": True},
        )
        s.add(t)
        s.flush()
        trade_id = t.id
    print(f"  inserted signal_id={sig_id} fake_shadow_trade_id={trade_id}")

    print(f"\n[3/6] Register Hyperliquid venue fetcher")
    venue = HyperliquidVenue()
    register_venue_fetcher("hyperliquid", make_fetcher(venue))
    print(f"  fetcher registered")

    print(f"\n[4/6] Call reconcile_once — should halt on detected drift")
    try:
        drifted = reconcile_once()
    except Exception as e:
        print(f"FAIL: reconcile_once raised: {e}", file=sys.stderr)
        # cleanup before exit
        _resume_all_open_halts("shakedown_recon_error")
        _delete_synthetic(trade_id, sig_id)
        sys.exit(1)
    drifted_assets = [(d.asset, d.drift_pct) for d in drifted]
    print(f"  drifted snapshots: {drifted_assets}")

    print(f"\n[5/6] Verify drift detected on {FAKE_ASSET} AND bot halted")
    drift_detected = any(asset == FAKE_ASSET for asset, _ in drifted_assets)
    halted = is_bot_halted(BOT_ID)
    print(f"  drift_detected_on_{FAKE_ASSET}={drift_detected}  is_bot_halted={halted}")
    passed = drift_detected and halted
    if passed:
        print(f"  PASS: reconciliation drift halt working")
    else:
        print(f"  FAIL: expected drift detection on {FAKE_ASSET} + halt, got drift={drift_detected} halted={halted}", file=sys.stderr)

    print(f"\n[6/6] Cleanup: resume halt + delete fake shadow trade")
    n_resumed = _resume_all_open_halts("shakedown_recon_cleanup")
    _delete_synthetic(trade_id, sig_id)
    print(f"  resumed {n_resumed} halt(s); deleted fake trade + signal")
    print(f"  is_bot_halted after cleanup={is_bot_halted(BOT_ID)}")

    write_audit("shakedown_reconciliation_test_complete", bot_id=BOT_ID,
                payload={"drifted_assets": drifted_assets, "passed": passed})

    print("\n" + "=" * 60)
    if passed:
        print("SHAKEDOWN GATE #5: PASS")
        sys.exit(0)
    else:
        print("SHAKEDOWN GATE #5: FAIL", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
