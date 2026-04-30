"""Scoring engine process entry point.

Cadence: every 15 minutes during paper phase. Cheap query against Postgres,
writes Score rows + DuckDB. Bots cannot read scores (separate process,
read-only access via DB grants when running on real Hetzner).
"""
import signal
import sys
import time
from typing import Any
from apscheduler.schedulers.background import BackgroundScheduler

from framework.logging_setup import configure_logging, get_logger
from framework.heartbeat import ping
from framework.scoring.engine import score_all_bots
from framework.audit import write_audit


PROCESS_NAME = "scoring"
SCORING_INTERVAL_MINUTES = 15


def _run() -> None:
    log = get_logger("framework.scoring.main")
    log.info("scoring_engine_starting")
    write_audit("scoring_engine_started")

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        lambda: ping(PROCESS_NAME, {"role": "scoring"}),
        "interval",
        seconds=30,
        id="scoring_heartbeat",
    )
    scheduler.add_job(
        score_all_bots,
        "interval",
        minutes=SCORING_INTERVAL_MINUTES,
        id="score_all_bots",
        next_run_time=None,  # run on schedule, not at boot
    )
    scheduler.start()
    score_all_bots()  # one immediate pass

    stop = False

    def _shutdown(*_args: Any) -> None:
        nonlocal stop
        log.info("scoring_engine_stopping")
        write_audit("scoring_engine_stopped")
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
        get_logger("framework.scoring.main").exception("scoring_crash")
        sys.exit(1)


if __name__ == "__main__":
    main()
