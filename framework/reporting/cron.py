"""7am-local cron driver for the daily report.

Spawned by the main framework process (or as its own service if we want to
isolate it). Uses APScheduler with a timezone-aware cron trigger so daylight
saving doesn't move the report time.
"""
import signal
import sys
import time
from typing import Any
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from framework.config import get_settings
from framework.logging_setup import configure_logging, get_logger
from framework.heartbeat import ping
from framework.reporting.sender import send_daily_report
from framework.audit import write_audit


PROCESS_NAME = "report_cron"


def _run() -> None:
    settings = get_settings()
    log = get_logger("framework.reporting.cron")
    log.info(
        "report_cron_starting",
        hour=settings.daily_report_hour_local,
        tz=settings.daily_report_timezone,
    )
    write_audit("report_cron_started")

    scheduler = BackgroundScheduler(timezone=settings.daily_report_timezone)
    scheduler.add_job(
        lambda: ping(PROCESS_NAME, {"role": "report_cron"}),
        "interval",
        seconds=30,
        id="cron_heartbeat",
    )
    scheduler.add_job(
        send_daily_report,
        CronTrigger(hour=settings.daily_report_hour_local, minute=0),
        id="daily_report",
    )
    scheduler.start()

    stop = False

    def _shutdown(*_args: Any) -> None:
        nonlocal stop
        log.info("report_cron_stopping")
        write_audit("report_cron_stopped")
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
        get_logger("framework.reporting.cron").exception("cron_crash")
        sys.exit(1)


if __name__ == "__main__":
    main()
