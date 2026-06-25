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
from framework.kill_criteria_monitor import check_all_bots_kill_criteria
# cross_bot_outcome_cron descheduled 2026-06-25 with STRUCTURE decommission — it
# only marked outcomes for STRUCTURE's cross-bot signal journal (which no longer
# writes) and was failing every 4h on a Hyperliquid SDK call. Re-import + re-add
# the job below to revive alongside STRUCTURE.
from framework.macro_monitor import check_macro_kill_switch, check_geo_shock_alert
from framework.stale_position_cleanup import check_and_close_stale_positions
from framework.audit import write_audit


PROCESS_NAME = "scoring"
SCORING_INTERVAL_MINUTES = 15
DD_CHECK_INTERVAL_MINUTES = 5  # tighter cadence than scoring; halts respond fast
KILL_CRITERIA_INTERVAL_MINUTES = 60  # hourly is fine — WR doesn't move in 15min
CROSS_BOT_OUTCOME_INTERVAL_MINUTES = 240  # every 4h aligns with 4h horizon
MACRO_KILL_SWITCH_INTERVAL_MINUTES = 5   # high-urgency — same cadence as dd_monitor
GEO_SHOCK_ALERT_INTERVAL_MINUTES = 60    # research-grade, slower changes
STALE_POSITION_CLEANUP_INTERVAL_MINUTES = 30  # half-hourly — prevents drift halt bug


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
    scheduler.add_job(
        check_all_bots_kill_criteria,
        "interval",
        minutes=KILL_CRITERIA_INTERVAL_MINUTES,
        id="kill_criteria_monitor",
    )
    scheduler.add_job(
        check_macro_kill_switch,
        "interval",
        minutes=MACRO_KILL_SWITCH_INTERVAL_MINUTES,
        id="macro_kill_switch",
    )
    scheduler.add_job(
        check_geo_shock_alert,
        "interval",
        minutes=GEO_SHOCK_ALERT_INTERVAL_MINUTES,
        id="geo_shock_alert",
    )
    scheduler.add_job(
        check_and_close_stale_positions,
        "interval",
        minutes=STALE_POSITION_CLEANUP_INTERVAL_MINUTES,
        id="stale_position_cleanup",
    )
    scheduler.start()
    score_all_bots()  # one immediate pass
    check_all_bots_dd()  # one immediate DD check
    check_all_bots_kill_criteria()  # one immediate kill-criteria check
    check_macro_kill_switch()  # one immediate macro shock check
    check_geo_shock_alert()  # one immediate geo-shock alert check

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
