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
from framework.dd_monitor import check_all_bots_dd
from framework.audit import write_audit


PROCESS_NAME = "scoring"
SCORING_INTERVAL_MINUTES = 15
DD_CHECK_INTERVAL_MINUTES = 5  # tighter cadence than scoring; halts respond fast


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
    # NOTE: do NOT pass next_run_time=None here — that means "add the job as
    # paused" in APScheduler, NOT "schedule the next run via the trigger."
    # The earlier comment "run on schedule, not at boot" was wrong: passing
    # next_run_time=None silently disabled both jobs for 3 days. The startup
    # behavior we actually want — run-now-then-on-interval — is achieved by
    # the explicit calls below.
    scheduler.add_job(
        score_all_bots,
        "interval",
        minutes=SCORING_INTERVAL_MINUTES,
        id="score_all_bots",
    )
    scheduler.add_job(
        check_all_bots_dd,
        "interval",
        minutes=DD_CHECK_INTERVAL_MINUTES,
        id="dd_monitor",
    )
    scheduler.start()
    score_all_bots()  # one immediate pass
    check_all_bots_dd()  # one immediate DD check

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
