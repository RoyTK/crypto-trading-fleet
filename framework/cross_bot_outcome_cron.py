"""Cross-bot signal outcome evaluator.

Runs every 4h via the scoring engine's APScheduler. For each row in
cross_bot_signal_log:
- If entry_price_usd is NULL: fetch current HL mid as approximate entry
- For each unfilled horizon (4h/12h/24h): if enough time has elapsed
  since event_timestamp_ms, fetch current HL mid and write outcome
- Mark outcome_evaluated_at once ALL three horizons are filled

Designed to be idempotent — safe to run more often than 4h. The
HL price fetch is a single HTTP call (all_mids) regardless of how many
rows we process; cheap.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from framework.db import session_scope
from framework.logging_setup import get_logger
from framework.models import CrossBotSignalLog


log = get_logger(__name__)


HORIZONS_HOURS = [4, 12, 24]


def _fetch_hl_mids() -> dict[str, float]:
    """One HTTP call to HL Info API. Returns {asset: mid_price_usd}."""
    try:
        from bots.structure.venue import HyperliquidVenue
        venue = HyperliquidVenue()  # read-only; no agent key needed
        return venue.all_mids() or {}
    except Exception:
        log.exception("outcome_cron_venue_fetch_failed")
        return {}


def run_outcome_evaluation() -> None:
    """Called by scoring-engine APScheduler every 4h. Evaluates pending rows."""
    mids = _fetch_hl_mids()
    if not mids:
        log.warning("outcome_cron_no_mids_skip_run")
        return

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    now_dt = datetime.now(timezone.utc)
    evaluated = 0

    with session_scope() as s:
        pending = list(s.execute(
            select(CrossBotSignalLog).where(
                CrossBotSignalLog.outcome_evaluated_at.is_(None)
            )
        ).scalars())

        if not pending:
            log.info("outcome_cron_nothing_pending")
            return

        for row in pending:
            asset_mid = mids.get(row.hl_asset)
            if asset_mid is None:
                log.warning("outcome_cron_no_mid", asset=row.hl_asset)
                continue

            # First pass: fill entry price if missing
            if row.entry_price_usd is None:
                row.entry_price_usd = float(asset_mid)

            entry = row.entry_price_usd
            if entry is None or entry <= 0:
                continue

            all_filled = True
            for hours in HORIZONS_HOURS:
                horizon_ms = row.event_timestamp_ms + hours * 3600 * 1000
                if now_ms < horizon_ms:
                    all_filled = False
                    continue

                price_attr = f"price_at_{hours}h"
                if getattr(row, price_attr) is not None:
                    continue  # already evaluated

                price = float(asset_mid)
                pnl_pct = (price - entry) / entry * 100.0
                if row.direction == "short":
                    pnl_pct = -pnl_pct
                direction_correct = pnl_pct > 0.0

                setattr(row, price_attr, price)
                setattr(row, f"pnl_at_{hours}h_pct", pnl_pct)
                setattr(row, f"direction_correct_{hours}h", direction_correct)

            if all_filled:
                row.outcome_evaluated_at = now_dt

            evaluated += 1

    log.info("outcome_cron_done", pending=len(pending), evaluated=evaluated)


if __name__ == "__main__":
    run_outcome_evaluation()
