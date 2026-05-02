"""60-day inactive wallet auto-prune.

Locked Item #7: any wallet with zero activity in trailing 60 days drops
from the pool. Daily cron via the framework scheduler.

The prune doesn't auto-modify wallet_pool.json — same pattern as
quarterly_whale_refresh: emit a P2 alert summarizing prunes, write an
audit_log row, and let Roy review + apply manually.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiohttp

from bots.copy.config import WALLET_PRUNE_INACTIVE_DAYS, get_copy_settings
from bots.copy.venue.cielo import CieloClient
from bots.copy.venue.helius_solana import HeliusSolanaClient
from framework.alerts import emit_alert
from framework.audit import write_audit
from framework.logging_setup import get_logger
from monitoring.alerting.taxonomy import Severity


log = get_logger(__name__)

WALLET_POOL_PATH = Path(__file__).parent / "wallet_pool.json"


async def find_inactive_wallets() -> list[dict]:
    """Returns the subset of pooled wallets that had no activity in
    WALLET_PRUNE_INACTIVE_DAYS. Each entry is {address, chain, last_seen_at}."""
    pool = _load_pool()
    if not pool:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=WALLET_PRUNE_INACTIVE_DAYS)

    helius = HeliusSolanaClient()
    cielo = CieloClient()
    inactive: list[dict] = []

    async with aiohttp.ClientSession() as session:
        sol_price = await helius.fetch_sol_price_usd(session)
        for entry in pool:
            address = entry.get("address")
            chain = entry.get("chain")
            if not address or not chain:
                continue
            last_seen = await _last_activity(helius, cielo, session, address, chain, sol_price)
            if last_seen is None or last_seen < cutoff:
                inactive.append({
                    "address": address,
                    "chain": chain,
                    "last_seen_at": last_seen.isoformat() if last_seen else None,
                })
    return inactive


async def _last_activity(
    helius: HeliusSolanaClient,
    cielo: CieloClient,
    session: aiohttp.ClientSession,
    address: str,
    chain: str,
    sol_price: float,
) -> Optional[datetime]:
    if chain == "solana":
        events = await helius.fetch_recent_buys(session, address, sol_price, limit=5)
    else:
        events = await cielo.fetch_recent_buys(session, address, chain, limit=5)
    if not events:
        return None
    latest_ms = max(e.timestamp_ms for e in events)
    return datetime.fromtimestamp(latest_ms / 1000, tz=timezone.utc)


def _load_pool() -> list[dict]:
    if not WALLET_POOL_PATH.exists():
        log.warning("wallet_pool_not_found", path=str(WALLET_POOL_PATH))
        return []
    try:
        return json.loads(WALLET_POOL_PATH.read_text())
    except Exception:
        log.exception("wallet_pool_load_failed")
        return []


async def run_prune_check() -> None:
    """Daily entry point. Identifies inactive wallets and emits P2 alert."""
    settings = get_copy_settings()
    pool = _load_pool()
    inactive = await find_inactive_wallets()
    n_pool = len(pool)
    n_inactive = len(inactive)

    write_audit(
        "copy_wallet_prune_check",
        bot_id="copy",
        payload={
            "pool_size": n_pool,
            "inactive_count": n_inactive,
            "inactive_wallets": inactive,
            "threshold_days": WALLET_PRUNE_INACTIVE_DAYS,
        },
    )

    if n_inactive == 0:
        log.info("wallet_prune_no_inactive", pool_size=n_pool)
        return

    severity = Severity.P2 if n_inactive < n_pool * 0.2 else Severity.P1
    emit_alert(
        severity=severity,
        title=f"[copy] {n_inactive}/{n_pool} wallets flagged for prune",
        body=(
            f"{n_inactive} of {n_pool} pool wallets had no activity in trailing "
            f"{WALLET_PRUNE_INACTIVE_DAYS} days. Review the audit_log row "
            f"`copy_wallet_prune_check` and apply manually to wallet_pool.json."
        ),
        bot_id="copy",
        event_type="wallet_prune_proposed",
        metadata={"inactive_count": n_inactive, "pool_size": n_pool},
    )
