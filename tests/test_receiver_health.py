"""RB4 regression guard — webhook-receiver silent-ingestion-death detection.

The receiver can be "up" (Docker sees the process running, it 200s Helius) yet not
actually feeding the trading bot. Three failure modes, and a plain liveness check
catches only the first:
  (A) event loop dead/wedged            → generic heartbeat staleness (not tested here)
  (B) receiver's Redis publish broken   → buys silently dropped (redis_ok False)
  (C) Helius stopped delivering         → receiver healthy but starving (stale delivery)

These pin the two PURE decision helpers that drive (B)/(C): `health_verdict` (the
receiver's own /health → 503) and `receiver_alert_reason` (the watchdog's P1 alert).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from monitoring.webhook_receiver.main import health_verdict
from framework.watchdog import receiver_alert_reason

STALE = 1800.0  # 30 min — the default receiver_delivery_stale_seconds
NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


def _iso(seconds_ago: float) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


# ---- health_verdict (drives /health → 200/503) ---------------------------

def test_health_redis_down_is_unhealthy_regardless_of_delivery():
    # A fresh, recently-delivering receiver is still unhealthy if Redis is unreachable —
    # because it can't publish buys (mode B).
    healthy, reason = health_verdict(False, 5.0, 5000.0, STALE)
    assert healthy is False and reason == "redis_unreachable"


def test_health_recent_delivery_is_ok():
    healthy, reason = health_verdict(True, 10.0, 5000.0, STALE)
    assert healthy is True and reason == "ok"


def test_health_no_delivery_during_warmup_is_ok():
    # Just started, no delivery yet — must NOT flag before it's had time to receive one.
    healthy, reason = health_verdict(True, None, 60.0, STALE)
    assert healthy is True and reason == "ok"


def test_health_no_delivery_after_warmup_is_stalled():
    healthy, reason = health_verdict(True, None, STALE + 1, STALE)
    assert healthy is False and reason == "ingestion_stalled"


def test_health_stale_delivery_is_stalled():
    healthy, reason = health_verdict(True, STALE + 300, 6000.0, STALE)
    assert healthy is False and reason == "ingestion_stalled"


def test_health_delivery_at_threshold_is_ok():
    # Boundary: exactly at (not beyond) the threshold is still healthy.
    healthy, reason = health_verdict(True, STALE, 6000.0, STALE)
    assert healthy is True and reason == "ok"


# ---- receiver_alert_reason (drives the watchdog P1) ----------------------

def test_alert_redis_flag_false_takes_priority():
    meta = {"redis_ok": False, "last_delivery_at": _iso(5), "uptime_seconds": 5000}
    assert receiver_alert_reason(meta, STALE, NOW) == "receiver_redis_unreachable"


def test_alert_healthy_recent_delivery_is_none():
    meta = {"redis_ok": True, "last_delivery_at": _iso(30), "uptime_seconds": 5000}
    assert receiver_alert_reason(meta, STALE, NOW) is None


def test_alert_stale_delivery_flags_stalled():
    meta = {"redis_ok": True, "last_delivery_at": _iso(STALE + 60), "uptime_seconds": 6000}
    assert receiver_alert_reason(meta, STALE, NOW) == "receiver_ingestion_stalled"


def test_alert_no_delivery_young_process_is_none():
    meta = {"redis_ok": True, "last_delivery_at": None, "uptime_seconds": 120}
    assert receiver_alert_reason(meta, STALE, NOW) is None


def test_alert_no_delivery_old_process_flags_stalled():
    meta = {"redis_ok": True, "last_delivery_at": None, "uptime_seconds": STALE + 100}
    assert receiver_alert_reason(meta, STALE, NOW) == "receiver_ingestion_stalled"


def test_alert_empty_meta_is_none():
    # Row absent / no metadata — skip rather than false-alarm (install-time, not death).
    assert receiver_alert_reason({}, STALE, NOW) is None


def test_alert_naive_timestamp_is_handled():
    # Defensive: a naive ISO string (no tzinfo) must be treated as UTC, not crash.
    naive = (NOW - timedelta(seconds=STALE + 60)).replace(tzinfo=None).isoformat()
    meta = {"redis_ok": True, "last_delivery_at": naive, "uptime_seconds": 6000}
    assert receiver_alert_reason(meta, STALE, NOW) == "receiver_ingestion_stalled"
