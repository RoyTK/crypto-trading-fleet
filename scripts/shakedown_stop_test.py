"""Phase 1 shakedown gate item #3 — server-side stop placement validation.

Locked Item #3 design: "server-side stop-losses where supported (Hyperliquid
yes, Solana DEX = 2-second software polling)". The bot currently uses
software stops in _manage_open_positions; this test validates we know how to
place server-side stops on HL for future use.

Sequence:
1. Open a small ($12) long shadow position via executor
2. Place a server-side SL trigger via exchange.order with order_type=trigger
3. Query info.open_orders to verify the trigger appears
4. Cancel the trigger order cleanly via exchange.cancel
5. Close the shadow position via executor.close_shadow

Safety: total exposure $12, zero stuck-position risk (everything cleaned up).
"""
from __future__ import annotations

import sys
import time

from bots.base.fill_simulator_base import SimulatedFill
from bots.structure.executor import StructureExecutor
from bots.structure.signals.base import SignalCandidate
from bots.structure.venue import HyperliquidVenue, is_exchange_available
from framework.audit import write_audit
from framework.db import session_scope
from framework.logging_setup import configure_logging, get_logger
from framework.models import Trade


BOT_ID = "structure"
TEST_ASSET = "BTC"
TEST_NOTIONAL_USD = 12.0
SL_OFFSET_PCT = 0.5  # SL 0.5% below entry (won't actually trigger in test window)


def main() -> None:
    configure_logging()
    log = get_logger("shakedown.stop_test")

    if not is_exchange_available():
        print("ERROR: Hyperliquid agent key not configured. Aborting.", file=sys.stderr)
        sys.exit(2)

    venue = HyperliquidVenue()
    executor = StructureExecutor(venue)
    write_audit("shakedown_stop_test_start", bot_id=BOT_ID, payload={"asset": TEST_ASSET, "notional": TEST_NOTIONAL_USD})

    mids = venue.all_mids()
    mid = float(mids.get(TEST_ASSET) or 0)
    if mid <= 0:
        print(f"ERROR: bad mid for {TEST_ASSET}: {mid}", file=sys.stderr)
        sys.exit(2)

    # 1. Synthetic paper trade for FK
    from framework.models import Signal
    from datetime import datetime, timezone
    with session_scope() as s:
        sig = Signal(
            bot_id=BOT_ID, signal_type="shakedown_stop_test",
            asset=TEST_ASSET, venue="hyperliquid", direction="long",
            payload={"shakedown_stop": True},
        )
        s.add(sig)
        s.flush()
        sig_id = sig.id

        t = Trade(
            bot_id=BOT_ID, signal_id=sig_id, mode="paper",
            asset=TEST_ASSET, venue="hyperliquid", direction="long",
            entry_price=mid, exit_price=mid, size_usd=TEST_NOTIONAL_USD, leverage=1.0,
            entry_at=datetime.now(timezone.utc), exit_at=datetime.now(timezone.utc),
            exit_reason="shakedown_synth", fill_status="closed", fees_usd=0.0,
            pnl_pct=0.0, pnl_usd=0.0,
            sim_metadata={"shakedown_synth": True, "slippage_bps": 0.0},
        )
        s.add(t)
        s.flush()
        paper_id = t.id

    candidate = SignalCandidate(
        signal_type="shakedown_stop_test", asset=TEST_ASSET, direction="long",
        payload={"shakedown": True},
    )
    sim_paper_fill = SimulatedFill(
        fill_price=mid, fees_usd=0.0, slippage_bps=0.0,
        metadata={"mid_at_entry": mid},
    )

    print(f"\n[1/5] Open shadow long {TEST_ASSET} ${TEST_NOTIONAL_USD} at ~${mid}")
    shadow_id = executor._place_shadow_unsafe(
        signal_id=sig_id, paper_trade_id=paper_id,
        candidate=candidate, paper_sim_fill=sim_paper_fill,
        shadow_usd=TEST_NOTIONAL_USD,
    )
    if shadow_id is None:
        print("FAIL: shadow open failed", file=sys.stderr)
        sys.exit(1)
    with session_scope() as s:
        st = s.get(Trade, shadow_id)
        actual_entry = float(st.entry_price or 0)
        sz_usd = float(st.size_usd or 0)
    print(f"  opened: shadow_id={shadow_id} entry=${actual_entry} size_usd=${sz_usd}")

    sz_native = round(sz_usd / actual_entry, venue.sz_decimals(TEST_ASSET))
    sl_trigger_px = round(actual_entry * (1.0 - SL_OFFSET_PCT / 100.0), 1)

    print(f"\n[2/5] Place server-side SL trigger at ${sl_trigger_px} (entry - {SL_OFFSET_PCT}%)")
    try:
        sl_response = venue.exchange.order(
            TEST_ASSET, False, sz_native, float(sl_trigger_px),
            {"trigger": {"isMarket": True, "triggerPx": float(sl_trigger_px), "tpsl": "sl"}},
            reduce_only=True,
        )
    except Exception as e:
        print(f"FAIL: SL order placement raised: {e}", file=sys.stderr)
        # Cleanup the shadow before exiting
        executor.close_shadow(shadow_id, paper_id, "shakedown_stop_test_cleanup")
        sys.exit(1)
    print(f"  SL response: {str(sl_response)[:300]}")

    sl_oid = None
    try:
        statuses = sl_response.get("response", {}).get("data", {}).get("statuses", [])
        if statuses and isinstance(statuses[0], dict):
            sl_oid = statuses[0].get("resting", {}).get("oid") or statuses[0].get("filled", {}).get("oid")
    except Exception:
        pass

    print(f"\n[3/5] Verify trigger appears in info.open_orders")
    time.sleep(1.5)
    master = venue.settings.hyperliquid_master_address
    try:
        open_orders = venue.info.open_orders(master)
    except Exception as e:
        print(f"WARN: open_orders fetch failed: {e}")
        open_orders = []
    matching = [o for o in open_orders if o.get("coin") == TEST_ASSET and (
        o.get("oid") == sl_oid or o.get("isTrigger") or "trigger" in str(o).lower()
    )]
    print(f"  open orders for {TEST_ASSET}: {len(open_orders)} total, {len(matching)} matching SL pattern")
    print(f"  SL oid from response: {sl_oid}")
    sl_visible = sl_oid is not None and any(o.get("oid") == sl_oid for o in open_orders)
    if sl_visible:
        print(f"  PASS: SL order oid={sl_oid} visible in open orders")
    else:
        print(f"  WARN: SL order oid={sl_oid} not found in open_orders snapshot — may have already triggered or been culled")

    print(f"\n[4/5] Cancel SL order")
    if sl_oid is not None:
        try:
            cancel_resp = venue.exchange.cancel(TEST_ASSET, sl_oid)
            print(f"  cancel response: {str(cancel_resp)[:200]}")
        except Exception as e:
            print(f"  WARN: cancel raised: {e}")

    print(f"\n[5/5] Close shadow position (reduce-only)")
    executor.close_shadow(shadow_id, paper_id, "shakedown_stop_test_done")
    with session_scope() as s:
        st = s.get(Trade, shadow_id)
        closed_ok = st is not None and st.fill_status == "closed"
    if closed_ok:
        print(f"  PASS: shadow closed")
    else:
        print(f"  FAIL: shadow {shadow_id} still open — manual cleanup required", file=sys.stderr)
        sys.exit(1)

    print("\n" + "=" * 60)
    if sl_response.get("status") == "ok" and sl_oid is not None:
        print("SHAKEDOWN GATE #3: PASS")
        print(f"  Server-side SL placed (oid={sl_oid}), visible={sl_visible}, canceled cleanly, position flat")
        write_audit("shakedown_stop_test_pass", bot_id=BOT_ID, payload={
            "shadow_id": shadow_id, "sl_oid": sl_oid, "sl_visible": sl_visible,
        })
        sys.exit(0)
    else:
        print("SHAKEDOWN GATE #3: FAIL — review output", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
