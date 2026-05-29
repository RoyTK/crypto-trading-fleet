"""COPY signal shadow log — H1/H2 diagnostic infrastructure.

Per the adversarial team meeting 2026-05-28: COPY's WR=17.6% at N=34 is
not explainable by exit asymmetry alone (statistician's H3 simulation
predicts WR=44% under random walk + observed exits). An adverse-signal
hypothesis is required — either H1 (signal is random + adverse exit
structure) or H2 (signal direction is inverted).

This module captures, for every cluster fire (REGARDLESS of whether
the bot actually trades), the price trajectory at +30m/+1h/+4h/+12h
plus MFE (max favorable excursion) and MAE (max adverse excursion).

The H1/H2 discriminator (per Engineer's R2 verdict): one-sample
t-test on (price_4h / entry_price - 1) at N≥80 with ≥5 distinct
6-hour buckets represented. mean_ret < 0 with |t| > 2.03 → H2
confirmed (signal is inverted). Otherwise H1 (broken).

Design:
- write_fire() — synchronous insert at cluster-signal fire time. Uses
  passed-in entry_price (caller fetched from quoter or batch_price).
- poll_pending() — APScheduler-style update of all pending rows.
  Batches Birdeye multi_price calls; updates appropriate window
  columns based on fired_at elapsed time; computes MFE/MAE.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text

from framework.db import session_scope
from framework.logging_setup import get_logger
from framework.models import CopySignalShadowLog


log = get_logger(__name__)


# Window targets (hours after fire_at)
_WINDOW_HOURS = (("price_30m", 0.5), ("price_1h", 1.0),
                 ("price_4h", 4.0), ("price_12h", 12.0))
_COMPLETE_AFTER_HOURS = 12.5  # close out at 12.5h (cushion past 12h)


def write_fire(
    *,
    cluster_uuid: str,
    signal_id: Optional[int],
    token_mint: str,
    hl_asset_if_any: Optional[str],
    cluster_size: int,
    cluster_total_notional_usd: float,
    wallet_tier: str,
    entry_price: Optional[float],
    fired_at: Optional[datetime] = None,
) -> Optional[int]:
    """Insert one fire-time row. Returns id, or None if write fails."""
    fired_at = fired_at or datetime.now(timezone.utc)
    try:
        with session_scope() as s:
            row = CopySignalShadowLog(
                cluster_uuid=cluster_uuid,
                signal_id=signal_id,
                token_mint=token_mint,
                hl_asset_if_any=hl_asset_if_any,
                cluster_size=cluster_size,
                cluster_total_notional_usd=cluster_total_notional_usd,
                wallet_tier=wallet_tier,
                fired_at=fired_at,
                entry_price=entry_price,
                status="pending" if entry_price is not None else "token_dead",
            )
            s.add(row)
            s.flush()
            return int(row.id)
    except Exception:
        # Most likely cause: duplicate cluster_uuid (signal re-fired
        # within the 15min suppression window from cluster.py). Log
        # but don't propagate — shadow log is observability, not
        # load-bearing.
        log.warning("shadow_log_write_fire_failed", cluster_uuid=cluster_uuid)
        return None


def _select_pending() -> list[CopySignalShadowLog]:
    """Return rows still being polled (pending or partial).

    Returns ORM objects detached from session — caller manages updates
    in a separate session_scope. Bounded LIMIT to avoid huge fetches.
    """
    with session_scope() as s:
        rows = s.execute(text(
            """
            SELECT id, cluster_uuid, token_mint, fired_at, entry_price,
                   price_30m, price_1h, price_4h, price_12h,
                   mfe_pct, mae_pct, status
            FROM copy_signal_shadow_log
            WHERE status IN ('pending', 'partial')
            ORDER BY fired_at ASC LIMIT 500
            """
        )).all()
    return [
        {
            "id": int(r.id),
            "cluster_uuid": str(r.cluster_uuid),
            "token_mint": str(r.token_mint),
            "fired_at": r.fired_at,
            "entry_price": float(r.entry_price) if r.entry_price is not None else None,
            "price_30m": float(r.price_30m) if r.price_30m is not None else None,
            "price_1h": float(r.price_1h) if r.price_1h is not None else None,
            "price_4h": float(r.price_4h) if r.price_4h is not None else None,
            "price_12h": float(r.price_12h) if r.price_12h is not None else None,
            "mfe_pct": float(r.mfe_pct) if r.mfe_pct is not None else None,
            "mae_pct": float(r.mae_pct) if r.mae_pct is not None else None,
            "status": str(r.status),
        }
        for r in rows
    ]


def update_one(
    row: dict,
    current_price: Optional[float],
    now: Optional[datetime] = None,
) -> dict:
    """Pure function: given a row dict and current price, return the
    updated field dict. Caller writes to DB.

    Side-effect-free so we can test it.
    """
    now = now or datetime.now(timezone.utc)
    elapsed = (now - row["fired_at"]).total_seconds() / 3600.0

    updates: dict = {}
    entry = row["entry_price"]

    # If no entry price yet AND we have current price now, set it
    if entry is None and current_price is not None and current_price > 0:
        updates["entry_price"] = current_price
        entry = current_price

    if entry is None or entry <= 0 or current_price is None or current_price <= 0:
        # Skip — token may be dead or quote failed
        return updates

    # MFE / MAE — relative pct from entry
    pct = (current_price - entry) / entry * 100.0
    cur_mfe = row.get("mfe_pct")
    cur_mae = row.get("mae_pct")
    if cur_mfe is None or pct > cur_mfe:
        updates["mfe_pct"] = pct
        updates["mfe_at"] = now
    if cur_mae is None or pct < cur_mae:
        updates["mae_pct"] = pct
        updates["mae_at"] = now

    # Window columns — set if elapsed crosses the window AND column null
    for col, hours in _WINDOW_HOURS:
        if elapsed >= hours and row.get(col) is None:
            updates[col] = current_price

    # Status transition: partial once any window filled; complete at 12.5h
    if elapsed >= _COMPLETE_AFTER_HOURS:
        updates["status"] = "complete"
    elif "price_30m" in updates or row.get("price_30m") is not None:
        updates["status"] = "partial"

    return updates


def write_updates(row_id: int, updates: dict) -> None:
    """Apply update dict to the row. Skips if updates is empty."""
    if not updates:
        return
    set_clauses = ", ".join(f"{k} = :{k}" for k in updates.keys())
    sql = f"UPDATE copy_signal_shadow_log SET {set_clauses}, updated_at = NOW() WHERE id = :id"
    params = dict(updates)
    params["id"] = row_id
    try:
        with session_scope() as s:
            s.execute(text(sql), params)
    except Exception:
        log.exception("shadow_log_update_failed", row_id=row_id)


def make_cluster_uuid() -> str:
    """Stable cluster id for log + Redis bridge correlation."""
    return str(uuid.uuid4())
