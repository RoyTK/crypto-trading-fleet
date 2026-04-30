"""Heartbeat / liveness watchdog.

Each bot process periodically calls ping(). A separate watchdog reads
heartbeats and emits alerts or restart signals based on staleness.
"""
from datetime import datetime, timezone
from typing import Optional, Any
from framework.db import session_scope
from framework.models import Heartbeat


def ping(process_name: str, metadata: Optional[dict[str, Any]] = None) -> None:
    with session_scope() as s:
        hb = s.get(Heartbeat, process_name)
        now = datetime.now(timezone.utc)
        if hb is None:
            s.add(Heartbeat(process_name=process_name, last_ping_at=now, metadata_json=metadata))
        else:
            hb.last_ping_at = now
            hb.metadata_json = metadata


def last_ping(process_name: str) -> Optional[datetime]:
    with session_scope() as s:
        hb = s.get(Heartbeat, process_name)
        return hb.last_ping_at if hb else None


def stale_processes(alert_after_seconds: int) -> list[tuple[str, datetime, float]]:
    """Return list of (process_name, last_ping_at, seconds_silent) past threshold."""
    from sqlalchemy import select

    with session_scope() as s:
        rows = s.execute(select(Heartbeat)).scalars().all()
        now = datetime.now(timezone.utc)
        stale = []
        for hb in rows:
            silent = (now - hb.last_ping_at).total_seconds()
            if silent >= alert_after_seconds:
                stale.append((hb.process_name, hb.last_ping_at, silent))
        return stale
