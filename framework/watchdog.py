"""Heartbeat watchdog.

Runs in its own loop (or via APScheduler in main.py). Reads heartbeats table;
emits alerts at the configured staleness thresholds and triggers restart
attempts beyond the restart threshold.

Phase 0: alerts go to the alerting router stub via Redis pub/sub. Restart
strategy is "publish a restart signal"; the actual process restart is handled
by the supervisor (Docker `restart: unless-stopped` for now; consider
healthcheck-based restart later).

Debounce: per-process alert + restart-signal each fire at most once per
ALERT_DEBOUNCE_SECONDS / RESTART_DEBOUNCE_SECONDS via Redis SETEX keys.
Without this, a single stale heartbeat row produces an alert every
watchdog tick (every ~30s) — overwhelms Discord/Telegram and causes
notification fatigue (Roy will start ignoring real alerts).
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

# A genuinely-stale process is worth knowing about, but once per hour is
# plenty. Re-alert when the cooldown expires (or when Redis evicts the key,
# whichever first).
ALERT_DEBOUNCE_SECONDS = 3600
RESTART_DEBOUNCE_SECONDS = 600  # restart signals fire more often than alerts;
                                 # supervisor uses them to attempt recovery


def _claim_debounce_slot(r: "redis.Redis", key: str, ttl_seconds: int) -> bool:
    """Atomic 'first one through' check. Returns True if this caller should
    fire (and we set the cooldown key); False if a prior tick already fired.
    """
    try:
        # SET NX = only set if not exists; returns True on success, False on collision
        return bool(r.set(name=key, value="1", ex=ttl_seconds, nx=True))
    except Exception as e:
        # If Redis is unreachable, allow the alert through — better noisy than silent
        log.warning("debounce_check_failed", key=key, error=str(e))
        return True


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

        # Per-process alert debounce — don't re-fire the same staleness on every tick
        alert_key = f"watchdog:alert:{name}"
        if not _claim_debounce_slot(r, alert_key, ALERT_DEBOUNCE_SECONDS):
            continue  # already alerted in the last ALERT_DEBOUNCE_SECONDS

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
            restart_key = f"watchdog:restart:{name}"
            if not _claim_debounce_slot(r, restart_key, RESTART_DEBOUNCE_SECONDS):
                continue
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
