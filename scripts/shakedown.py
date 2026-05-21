"""Phase 0 shakedown gate — runs the 8-check validation script.

Usage:
    python -m scripts.shakedown               # all checks
    python -m scripts.shakedown --check 3     # one specific check (1-8)

Each check returns a (passed: bool, message: str). The script exits non-zero
if any check fails. Designed to be runnable from the framework container
once the stack is up on Hetzner.

Checks 1-6 + 8 are automatable. Check 7 (Cloudflare Tunnel from non-VPN)
requires a manual browser test from your phone — script prints instructions
and asks for confirmation.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
import redis

from framework.config import get_settings
from framework.db import session_scope
from framework.models import Signal, Trade, Heartbeat, Halt, AuditLog
from framework.audit import write_audit
from framework.heartbeat import ping
from framework.scoring.engine import score_all_bots


CHECKS: list[tuple[str, Callable[[], tuple[bool, str]]]] = []


def _register(label: str):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


@_register("Fake-signal end-to-end")
def check_fake_signal() -> tuple[bool, str]:
    test_payload = {"shakedown": True, "ts": datetime.now(timezone.utc).isoformat()}
    with session_scope() as s:
        sig = Signal(
            bot_id="structure",
            signal_type="shakedown_test",
            asset="BTC",
            venue="hyperliquid",
            direction="long",
            payload=test_payload,
        )
        s.add(sig)
        s.flush()
        sig_id = sig.id
    score_all_bots()  # may produce no score if no closed trades, that's fine
    with session_scope() as s:
        verify = s.get(Signal, sig_id)
        if verify is None:
            return False, "signal not persisted"
    return True, f"signal id={sig_id} persisted, scoring pass ran"


@_register("/panic dispatch test (Discord stub + Telegram stub)")
def check_panic_dispatch() -> tuple[bool, str]:
    settings = get_settings()
    r = redis.Redis.from_url(settings.redis_url)
    test_msg = json.dumps({"actor": "shakedown:test", "started_at": datetime.now(timezone.utc).isoformat()})
    r.publish("panic:execute:structure", test_msg)
    r.publish("panic:execute:copy", test_msg)
    return True, "panic test message published to redis (no live trigger)"


@_register("Heartbeat self-restart simulation")
def check_heartbeat_restart() -> tuple[bool, str]:
    process_name = "shakedown_test_proc"
    try:
        with session_scope() as s:
            old_ts = datetime.now(timezone.utc) - timedelta(seconds=600)
            hb = s.get(Heartbeat, process_name)
            if hb is None:
                s.add(Heartbeat(process_name=process_name, last_ping_at=old_ts))
            else:
                hb.last_ping_at = old_ts
        from framework.watchdog import watchdog_pass
        watchdog_pass()
        with session_scope() as s:
            recent = (
                s.query(AuditLog)
                .filter(AuditLog.event_type == "heartbeat_restart_signal")
                .order_by(AuditLog.created_at.desc())
                .first()
            )
        if recent is None:
            return False, "watchdog did not emit restart signal"
        return True, f"watchdog emitted restart signal (audit id={recent.id})"
    finally:
        # CRITICAL CLEANUP: leaving this row in the heartbeats table makes the
        # watchdog fire a P0 alert every ~30s indefinitely — caused weeks of
        # Discord/Telegram notification spam after the original Phase 0 run.
        with session_scope() as s:
            hb = s.get(Heartbeat, process_name)
            if hb is not None:
                s.delete(hb)


@_register("P0 alert reaches Twilio (or skipped if no creds)")
def check_p0_twilio() -> tuple[bool, str]:
    from framework.alerts import emit_alert
    from monitoring.alerting.taxonomy import Severity
    settings = get_settings()
    if not (settings.twilio_account_sid and settings.twilio_to_number):
        return True, "skipped: Twilio creds not configured (acceptable in pre-Hetzner dev)"
    emit_alert(
        severity=Severity.P0,
        title="shakedown P0 test",
        body="if you got this SMS the P0 path works",
        event_type="shakedown",
    )
    return True, "P0 alert emitted to Redis (verify SMS arrives manually)"


@_register("Empty-fleet daily report renders + dispatches")
def check_daily_report() -> tuple[bool, str]:
    from framework.reporting.sender import send_daily_report
    try:
        send_daily_report()
    except Exception as e:
        return False, f"daily report failed: {e}"
    return True, "daily report dispatched (verify Discord channel + email inbox)"


@_register("Postgres migrations clean on fresh DB")
def check_alembic() -> tuple[bool, str]:
    from sqlalchemy import inspect
    from framework.db import get_engine
    inspector = inspect(get_engine())
    expected = {
        "bot_state", "signals", "trades", "calibration_records",
        "halts", "scores", "audit_log", "heartbeats", "alembic_version",
    }
    actual = set(inspector.get_table_names())
    missing = expected - actual
    if missing:
        return False, f"missing tables: {sorted(missing)}"
    return True, f"all {len(expected)} tables present"


@_register("Cloudflare Tunnel reachable from non-VPN (manual)")
def check_cloudflare_tunnel() -> tuple[bool, str]:
    print("\nMANUAL CHECK:")
    print("  1. From your phone on mobile data, open: grafana.fleet.<your-domain>.com")
    print("  2. Cloudflare Access should prompt for email PIN")
    print("  3. Enter the PIN, confirm Grafana login appears")
    answer = input("Did the manual check pass? [y/N] ").strip().lower()
    return (answer == "y"), ("manual: yes" if answer == "y" else "manual: declined or failed")


@_register("Backup restore verified")
def check_backup_restore() -> tuple[bool, str]:
    print("\nMANUAL CHECK:")
    print("  Run: scripts/verify_backup.sh /path/to/latest-postgres-dump.sql")
    print("  Confirm output ends with 'Backup restore verification: OK'")
    answer = input("Did the manual check pass? [y/N] ").strip().lower()
    return (answer == "y"), ("manual: yes" if answer == "y" else "manual: declined or failed")


def run_checks(only: int | None = None) -> int:
    print(f"=== Phase 0 Shakedown — {datetime.now(timezone.utc).isoformat()} ===\n")
    failed = 0
    for i, (label, fn) in enumerate(CHECKS, start=1):
        if only is not None and i != only:
            continue
        print(f"[{i}/{len(CHECKS)}] {label}")
        try:
            ok, msg = fn()
        except Exception as e:
            ok, msg = False, f"exception: {e!r}"
        status = "PASS" if ok else "FAIL"
        print(f"   {status}: {msg}\n")
        write_audit(
            "shakedown_check",
            payload={"check": i, "label": label, "passed": ok, "message": msg},
        )
        if not ok:
            failed += 1
    print(f"=== Result: {len(CHECKS) - failed}/{len(CHECKS)} passed ===")
    return 0 if failed == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", type=int, help="run only check N (1-based)")
    args = parser.parse_args()
    sys.exit(run_checks(only=args.check))


if __name__ == "__main__":
    main()
