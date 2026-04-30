"""Framework supervisor entry point.

Phase 0 responsibilities:
- Run heartbeat watchdog on a 30s cadence
- Run position reconciliation on a 5min cadence
- Emit own heartbeat ('framework') so the watchdog watches the watchdog
- Bind seed bot_state rows for the 4 bots in 'initializing' state on first boot
"""
import signal
import sys
import time
from typing import Any
from apscheduler.schedulers.background import BackgroundScheduler

from framework.config import get_settings
from framework.logging_setup import configure_logging, get_logger
from framework.db import session_scope
from framework.models import BotState
from framework.heartbeat import ping
from framework.watchdog import watchdog_pass
from framework.reconciliation import reconcile_once
from framework.audit import write_audit


PROCESS_NAME = "framework"
BOT_IDS = ("structure", "copy", "event", "sniper")


def _ensure_bot_state_rows() -> None:
    with session_scope() as s:
        existing = {b.bot_id for b in s.query(BotState).all()}
        for bot_id in BOT_IDS:
            if bot_id not in existing:
                s.add(BotState(bot_id=bot_id, state="initializing"))


def _run() -> None:
    settings = get_settings()
    log = get_logger("framework.main")
    log.info("framework_starting", env="phase0")

    _ensure_bot_state_rows()
    write_audit("framework_started", payload={"version": "phase0"})

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        lambda: ping(PROCESS_NAME, {"role": "supervisor"}),
        "interval",
        seconds=settings.heartbeat_interval_seconds,
        id="framework_heartbeat",
    )
    scheduler.add_job(
        watchdog_pass,
        "interval",
        seconds=settings.heartbeat_interval_seconds,
        id="watchdog",
    )
    scheduler.add_job(
        reconcile_once,
        "interval",
        seconds=settings.reconciliation_interval_seconds,
        id="reconciliation",
    )
    scheduler.start()

    stop = False

    def _shutdown(*_args: Any) -> None:
        nonlocal stop
        log.info("framework_stopping")
        write_audit("framework_stopped")
        stop = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while not stop:
        time.sleep(1)

    scheduler.shutdown(wait=False)


def main() -> None:
    configure_logging()
    try:
        _run()
    except Exception:
        get_logger("framework.main").exception("framework_crash")
        sys.exit(1)


if __name__ == "__main__":
    main()
