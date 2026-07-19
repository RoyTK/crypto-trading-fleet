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
from typing import Optional
import redis
from framework.config import get_settings
from framework.heartbeat import stale_processes
from framework.audit import write_audit
from framework.halt_state import halt_bot
from framework.alert_emit import emit_alert
from framework.logging_setup import get_logger

log = get_logger(__name__)

ALERT_CHANNEL = "alerts:heartbeat"
ALERT_EMIT_CHANNEL = "alerts:emit"
RESTART_CHANNEL = "supervisor:restart"

# RB4 — the webhook receiver publishes this heartbeat (see monitoring/webhook_receiver).
# Its loop-alive staleness is handled by the generic stale_processes() path below; its
# INGESTION health (Redis publish broken / Helius delivery starvation) lives in the
# heartbeat metadata and is checked by receiver_ingestion_pass().
RECEIVER_PROCESS_NAME = "receiver:webhook"
# Ingestion alerts are the slow, quiet kind — re-remind at most every 30 min.
RECEIVER_ALERT_DEBOUNCE_SECONDS = 1800

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


def _emit_alert(r: "redis.Redis", severity: str, title: str, body: str,
                event_type: str, metadata: dict) -> None:
    """Emit a general-purpose alert. Delegates to the shared emit_alert so P0/P1
    alerts get the RB3 direct-Discord fallback when the dispatcher is unreachable."""
    emit_alert(severity, title, body, event_type=event_type,
               metadata=metadata, redis_client=r)


def receiver_alert_reason(meta: dict, stale_after: float, now: datetime) -> Optional[str]:
    """Pure decision for the two silent-ingestion-death modes. Returns the alert
    event_type, or None if the receiver looks healthy. Factored out so the logic is
    unit-testable without a DB/Redis.

    (B) redis_ok is False → receiver can't reach Redis, so matched buys are NOT
        published to the bot even though Helius deliveries keep returning 200 OK.
    (C) no delivery for > stale_after → Helius stopped sending. When no delivery has
        been seen yet this process life (last_delivery_at is None), only flag it once
        the process has itself been up longer than stale_after (else a fresh start
        false-alarms before its first delivery).
    """
    if not meta:
        return None
    if meta.get("redis_ok") is False:
        return "receiver_redis_unreachable"

    last_iso = meta.get("last_delivery_at")
    silent: Optional[float] = None
    if last_iso:
        try:
            last = datetime.fromisoformat(last_iso)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            silent = (now - last).total_seconds()
        except Exception:
            silent = None

    uptime = float(meta.get("uptime_seconds") or 0.0)
    starved = (silent is not None and silent > stale_after) or (
        silent is None and uptime >= stale_after
    )
    return "receiver_ingestion_stalled" if starved else None


def receiver_ingestion_pass(r: "redis.Redis") -> None:
    """Alert on the receiver's silent-ingestion-death modes (see receiver_alert_reason).

    Reads the receiver's own heartbeat metadata (written every ~30s by the receiver)
    and emits a debounced P1. If the receiver never pinged (row absent), the generic
    loop-alive path can't see it either — that's an install-time problem, not a silent
    death, so we skip rather than false-alarm.
    """
    from framework.db import session_scope
    from framework.models import Heartbeat

    stale_after = get_settings().receiver_delivery_stale_seconds
    with session_scope() as sess:
        hb = sess.get(Heartbeat, RECEIVER_PROCESS_NAME)
        meta = dict(hb.metadata_json or {}) if hb is not None else None
    if not meta:
        return

    reason = receiver_alert_reason(meta, stale_after, datetime.now(timezone.utc))
    if reason == "receiver_redis_unreachable":
        if _claim_debounce_slot(r, "watchdog:receiver_redis", RECEIVER_ALERT_DEBOUNCE_SECONDS):
            _emit_alert(
                r, "p1", "receiver Redis unreachable",
                "The webhook receiver cannot reach Redis — matched wallet buys are NOT "
                "being published to the trading bot (Helius deliveries still 200 OK, so "
                "this is silent). Ingestion is effectively dead until Redis recovers.",
                reason, meta,
            )
    elif reason == "receiver_ingestion_stalled":
        if _claim_debounce_slot(
            r, "watchdog:receiver_ingestion", RECEIVER_ALERT_DEBOUNCE_SECONDS
        ):
            last_iso = meta.get("last_delivery_at")
            silent_txt = "no delivery since startup" if not last_iso else f"since {last_iso}"
            _emit_alert(
                r, "p1", "receiver ingestion stalled",
                f"The webhook receiver has ingested no Helius deliveries ({silent_txt}, "
                f"threshold {stale_after}s). Likely a stopped/deleted webhook, a rotated "
                f"HELIUS_WEBHOOK_AUTH_SECRET, or a Helius outage. Wallet copy signals are "
                f"not arriving.",
                reason, meta,
            )


def watchdog_pass() -> None:
    s = get_settings()
    alert_after = s.heartbeat_alert_after_seconds
    restart_after = s.heartbeat_restart_after_seconds

    r = redis.Redis.from_url(s.redis_url)

    # RB4: receiver ingestion health (runs every tick, independent of generic staleness).
    try:
        receiver_ingestion_pass(r)
    except Exception:
        log.exception("receiver_ingestion_pass_failed")

    stale = stale_processes(alert_after_seconds=alert_after)
    if not stale:
        return

    for name, last_ping_at, silent in stale:
        severity = "p1" if silent < restart_after else "p0"

        # Per-process alert debounce — don't re-fire the same staleness on every tick
        alert_key = f"watchdog:alert:{name}"
        if not _claim_debounce_slot(r, alert_key, ALERT_DEBOUNCE_SECONDS):
            continue  # already alerted in the last ALERT_DEBOUNCE_SECONDS

        # RB3: route through emit_alert so a P0/P1 staleness alert reaches Discord even
        # when the DISPATCHER is the dead process (the self-referential SPOF: the
        # watchdog would otherwise be reporting the dispatcher's death through the dead
        # dispatcher). emit_alert falls back to a direct bot-token POST on 0 subscribers.
        try:
            emit_alert(
                severity,
                f"heartbeat silent: {name}",
                f"{name} has been silent for {silent:.0f}s "
                f"(last ping {last_ping_at.isoformat()}).",
                event_type="heartbeat",
                metadata={
                    "process": name,
                    "silent_seconds": silent,
                    "last_ping_at": last_ping_at.isoformat(),
                },
                redis_client=r,
            )
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
