"""Heartbeat watchdog.

Runs in its own loop (or via APScheduler in main.py). Reads heartbeats table;
emits alerts at the configured staleness thresholds and triggers restart
attempts beyond the restart threshold.

Phase 0: alerts go to the alerting router stub via Redis pub/sub. Restart
strategy is "publish a restart signal"; the actual process restart is handled
by the supervisor (Docker `restart: unless-stopped` for now; consider
healthcheck-based restart later).
"""
import json
from datetime import datetime, timezone
import redis
from framework.config import get_settings
from framework.heartbeat import stale_processes
from framework.audit import write_audit
from framework.halt_state import halt_bot
from framework.logging_setup import get_logger

log = get_logger(__name__)

ALERT_CHANNEL = "alerts:heartbeat"
RESTART_CHANNEL = "supervisor:restart"


def watchdog_pass() -> None:
    s = get_settings()
    alert_after = s.heartbeat_alert_after_seconds
    restart_after = s.heartbeat_restart_after_seconds

    stale = stale_processes(alert_after_seconds=alert_after)
    if not stale:
        return

    r = redis.Redis.from_url(s.redis_url)
    for name, last_ping_at, silent in stale:
        severity = "p1" if silent < restart_after else "p0"
        payload = {
            "process": name,
            "last_ping_at": last_ping_at.isoformat(),
            "silent_seconds": silent,
            "severity": severity,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        try:
            r.publish(ALERT_CHANNEL, json.dumps(payload))
        except Exception as e:
            log.warning("alert_publish_failed", error=str(e))

        if silent >= restart_after:
            try:
                r.publish(RESTART_CHANNEL, json.dumps({"process": name}))
            except Exception as e:
                log.warning("restart_publish_failed", error=str(e))
            write_audit(
                "heartbeat_restart_signal",
                payload={"process": name, "silent_seconds": silent},
            )

            if name.startswith("bot:"):
                bot_id = name.split(":", 1)[1]
                halt_bot(
                    bot_id=bot_id,
                    halt_type="heartbeat",
                    reason=f"heartbeat silent {silent:.0f}s exceeded restart threshold",
                    severity="p0",
                    metadata={"silent_seconds": silent},
                )
