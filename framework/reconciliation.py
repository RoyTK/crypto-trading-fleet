"""Position reconciliation framework.

Every 5 minutes (configurable), each bot's executor reports its tracked
positions; this module compares against venue-actual positions reported via the
same registry. Drift > 0.5% (configurable) triggers a per-bot halt + P1 alert.

Phase 0: cron skeleton only. Per-venue actual fetchers are stubbed; activate
when each bot's build phase wires its executor in.
"""
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional, Callable
from framework.config import get_settings
from framework.audit import write_audit
from framework.halt_state import halt_bot
from framework.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class PositionSnapshot:
    bot_id: str
    asset: str
    venue: str
    bot_size: float    # what the bot thinks it holds
    venue_size: float  # what the venue reports
    drift_pct: float   # 100 * |bot - venue| / max(|venue|, epsilon)


# Registry of venue-actual fetchers; each bot's build phase registers its own.
_VENUE_FETCHERS: dict[str, Callable] = {}


def register_venue_fetcher(venue: str, fetcher: Callable) -> None:
    _VENUE_FETCHERS[venue] = fetcher


def reconcile_once() -> list[PositionSnapshot]:
    """Run one reconciliation pass. Returns drift snapshots that exceeded threshold."""
    threshold = get_settings().reconciliation_drift_threshold_pct
    drifted: list[PositionSnapshot] = []

    if not _VENUE_FETCHERS:
        log.debug("reconcile_skipped", reason="no fetchers registered")
        return drifted

    for venue, fetcher in _VENUE_FETCHERS.items():
        try:
            snapshots: list[PositionSnapshot] = fetcher()
        except Exception as e:
            log.warning("reconcile_fetcher_error", venue=venue, error=str(e))
            continue

        for snap in snapshots:
            if snap.drift_pct >= threshold:
                drifted.append(snap)
                log.warning(
                    "position_drift",
                    bot=snap.bot_id, venue=snap.venue, asset=snap.asset,
                    bot_size=snap.bot_size, venue_size=snap.venue_size,
                    drift_pct=snap.drift_pct,
                )
                halt_bot(
                    snap.bot_id,
                    halt_type="drift",
                    reason=f"position drift {snap.drift_pct:.2f}% on {snap.venue}/{snap.asset}",
                    severity="p1",
                    metadata={
                        "venue": snap.venue,
                        "asset": snap.asset,
                        "bot_size": snap.bot_size,
                        "venue_size": snap.venue_size,
                        "drift_pct": snap.drift_pct,
                    },
                )
                write_audit(
                    "position_drift_halt",
                    bot_id=snap.bot_id,
                    payload={
                        "venue": snap.venue, "asset": snap.asset,
                        "drift_pct": snap.drift_pct,
                    },
                )

    return drifted
