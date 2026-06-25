"""Daily wallet-pool reconciliation cron.

Runs every day at ~07:00 UTC (cron-installed). For each pool wallet:
1. Recompute events_30d from wallet_events_log
2. Decide promote/demote/drop transitions (wallet_pool_manager.decide_tier_changes)
3. Apply transitions to wallet_pool table
4. Sync to Helius (both active + watch webhooks) via sync_pool_tiers()
5. Truncate wallet_events_log rows older than 90 days
6. Emit P2 alert summarizing transitions (Discord-only)
7. Write audit_log row

Idempotent: re-running on the same day is a no-op (or noisy-equivalent —
already-fresh events_30d gets recomputed, decisions re-evaluated, Helius
sync re-runs as noop if no addresses changed).
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import aiohttp
from sqlalchemy import select, text

from bots.copy.config import get_copy_settings
from bots.copy.venue.helius_webhooks import sync_pool_tiers
from bots.copy.wallet_pool_manager import (
    WalletSnapshot,
    decide_tier_changes,
)
from framework.alerts import emit_alert
from framework.audit import write_audit
from framework.db import session_scope
from framework.logging_setup import configure_logging, get_logger
from framework.models import WalletPool
from monitoring.alerting.taxonomy import Severity


log = get_logger("wallet_pool_daily_cron")

EVENT_LOG_RETENTION_DAYS = 90


def _recompute_event_counts() -> None:
    """For every wallet, refresh events_30d from wallet_events_log."""
    with session_scope() as s:
        s.execute(text("""
            UPDATE wallet_pool SET events_30d = sub.cnt FROM (
                SELECT wallet_address, COUNT(*) AS cnt
                FROM wallet_events_log
                WHERE event_at >= NOW() - INTERVAL '30 days'
                GROUP BY wallet_address
            ) AS sub
            WHERE wallet_pool.address = sub.wallet_address
        """))
        # Zero out wallets that had events previously but none in last 30d
        s.execute(text("""
            UPDATE wallet_pool SET events_30d = 0
            WHERE address NOT IN (
                SELECT DISTINCT wallet_address FROM wallet_events_log
                WHERE event_at >= NOW() - INTERVAL '30 days'
            )
        """))


def _refresh_attribution_timestamps() -> None:
    """For each wallet, set last_attribution_at = max(created_at) over
    wallet_attributions where attributed_pnl_usd > 0. Used by the
    1-year demotion-protection rule.
    """
    with session_scope() as s:
        s.execute(text("""
            UPDATE wallet_pool SET last_attribution_at = sub.last_pos FROM (
                SELECT wallet_address, MAX(created_at) AS last_pos
                FROM wallet_attributions
                WHERE attributed_pnl_usd > 0
                GROUP BY wallet_address
            ) AS sub
            WHERE wallet_pool.address = sub.wallet_address
        """))


def _snapshot_wallets() -> list[WalletSnapshot]:
    """Load wallet_pool rows + auxiliary event counts (7d, 48h) for the manager."""
    snapshots: list[WalletSnapshot] = []
    with session_scope() as s:
        # Aux counts per wallet (single roundtrip)
        rows = s.execute(text("""
            SELECT address, events_7d, events_48h FROM (
                SELECT wp.address,
                       COALESCE(SUM(CASE WHEN wel.event_at >= NOW() - INTERVAL '7 days' THEN 1 ELSE 0 END), 0) AS events_7d,
                       COALESCE(SUM(CASE WHEN wel.event_at >= NOW() - INTERVAL '48 hours' THEN 1 ELSE 0 END), 0) AS events_48h
                FROM wallet_pool wp
                LEFT JOIN wallet_events_log wel ON wel.wallet_address = wp.address
                GROUP BY wp.address
            ) AS aux
        """)).all()
        aux: dict[str, tuple[int, int]] = {r.address: (int(r.events_7d), int(r.events_48h)) for r in rows}

        # Per-wallet attributed PnL (2026-06-21) — drives PnL-based demotion.
        # COUNT = number of closed copied trades this wallet participated in;
        # SUM = its equal-share attributed PnL. Only 'copy' bot rows.
        attr_rows = s.execute(text("""
            SELECT wallet_address,
                   COUNT(*) AS n,
                   COALESCE(SUM(attributed_pnl_usd), 0) AS pnl,
                   COALESCE(MIN(attributed_pnl_usd), 0) AS worst,
                   COUNT(*) FILTER (WHERE attributed_pnl_usd > 0) AS wins
            FROM wallet_attributions
            WHERE bot_id = 'copy'
            GROUP BY wallet_address
        """)).all()
        attr: dict[str, tuple[int, float, float, int]] = {
            r.wallet_address: (int(r.n), float(r.pnl), float(r.worst), int(r.wins))
            for r in attr_rows
        }

        for w in s.execute(select(WalletPool)).scalars():
            events_7d, events_48h = aux.get(w.address, (0, 0))
            attributed_trades, attributed_pnl_usd, worst_attributed, attributed_wins = attr.get(
                w.address, (0, 0.0, 0.0, 0))
            snapshots.append(WalletSnapshot(
                address=w.address,
                tier=w.tier,
                added_at=w.added_at,
                last_event_at=w.last_event_at,
                events_30d=int(w.events_30d or 0),
                events_7d=events_7d,
                events_48h=events_48h,
                cielo_winrate_90d=(
                    (w.cielo_winrate_90d / 100.0) if (w.cielo_winrate_90d or 0) > 1.0
                    else w.cielo_winrate_90d
                ),
                last_attribution_at=w.last_attribution_at,
                pinned=bool(w.pinned),
                attributed_trades=attributed_trades,
                attributed_pnl_usd=attributed_pnl_usd,
                worst_attributed_pnl_usd=worst_attributed,
                attributed_wins=attributed_wins,
                source=w.source,
            ))
    return snapshots


def _apply_transitions(decisions) -> None:
    """Write tier changes to wallet_pool + log to audit_log."""
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        # Promotions: watch → active
        for addr in decisions.promote:
            w = s.get(WalletPool, addr)
            if w is None or w.tier != "watch":
                continue
            w.tier = "active"
            w.promoted_at = now
        # Demotions: active → watch
        for addr in decisions.demote:
            w = s.get(WalletPool, addr)
            if w is None or w.tier != "active":
                continue
            w.tier = "watch"
            w.demoted_at = now
        # Drops: watch → pruned
        for addr in decisions.drop:
            w = s.get(WalletPool, addr)
            if w is None or w.tier != "watch":
                continue
            w.tier = "pruned"
            w.demoted_at = now
        # Swap-ins: paired (promote_addr, demote_addr)
        for promote_addr, demote_addr in decisions.swap_in:
            wp = s.get(WalletPool, promote_addr)
            wd = s.get(WalletPool, demote_addr)
            if wp is not None and wp.tier == "watch":
                wp.tier = "active"
                wp.promoted_at = now
            if wd is not None and wd.tier == "active":
                wd.tier = "watch"
                wd.demoted_at = now

    write_audit(
        "wallet_pool_daily_transitions",
        payload={
            "promoted": decisions.promote,
            "demoted": decisions.demote,
            "dropped": decisions.drop,
            "swapped": decisions.swap_in,
        },
    )


def _truncate_event_log() -> int:
    """Delete wallet_events_log rows older than EVENT_LOG_RETENTION_DAYS. Returns rows deleted."""
    with session_scope() as s:
        r = s.execute(text("""
            DELETE FROM wallet_events_log
            WHERE event_at < NOW() - INTERVAL '%(d)s days'
        """ % {"d": EVENT_LOG_RETENTION_DAYS}))
        return r.rowcount or 0


async def _sync_helius(api_key: str, auth_secret: str, active_url: str, watch_url: str) -> dict:
    with session_scope() as s:
        active = [w.address for w in s.execute(select(WalletPool).where(
            WalletPool.chain == "solana", WalletPool.tier == "active"
        )).scalars()]
        watch = [w.address for w in s.execute(select(WalletPool).where(
            WalletPool.chain == "solana", WalletPool.tier == "watch"
        )).scalars()]
    async with aiohttp.ClientSession() as session:
        return await sync_pool_tiers(
            session, api_key,
            active_addresses=active,
            watch_addresses=watch,
            active_url=active_url,
            watch_url=watch_url,
            auth_header=auth_secret,
        )


def main() -> int:
    configure_logging()
    settings = get_copy_settings()

    api_key = settings.helius_api_key
    auth_secret = os.environ.get("HELIUS_WEBHOOK_AUTH_SECRET", "")
    active_url = os.environ.get("COPY_WEBHOOK_URL", "")
    watch_url = os.environ.get("COPY_WEBHOOK_URL_WATCH", "")

    log.info("daily_cron_start",
             has_helius_key=bool(api_key),
             has_auth_secret=bool(auth_secret),
             has_active_url=bool(active_url),
             has_watch_url=bool(watch_url))

    # Data accumulation always runs — even without Helius sync configured,
    # we want events_30d to stay fresh so the dashboard reflects reality.
    _recompute_event_counts()
    _refresh_attribution_timestamps()
    snapshots = _snapshot_wallets()
    decisions = decide_tier_changes(
        snapshots,
        active_list_target=settings.copy_active_list_target,
        promote_vetted_only=settings.copy_promote_vetted_only,
    )

    log.info("decisions",
             promote=len(decisions.promote),
             demote=len(decisions.demote),
             drop=len(decisions.drop),
             swap_in=len(decisions.swap_in))

    if (decisions.promote or decisions.demote or decisions.drop or decisions.swap_in):
        _apply_transitions(decisions)
        # Helius sync requires all four env vars. If watch_url isn't set yet
        # (initial deploy), skip the sync but keep the DB transitions —
        # they're still useful for dashboards + the next run will retry.
        if api_key and auth_secret and active_url and watch_url:
            try:
                sync_result = asyncio.run(_sync_helius(api_key, auth_secret, active_url, watch_url))
                log.info("helius_synced", **{k: v[1] for k, v in sync_result.items()})
            except Exception:
                log.exception("helius_sync_failed")
        else:
            log.warning("helius_sync_skipped",
                        reason="missing env (likely COPY_WEBHOOK_URL_WATCH)")

        # Active count after transitions
        with session_scope() as s:
            n_active = s.execute(text(
                "SELECT COUNT(*) FROM wallet_pool WHERE tier='active'"
            )).scalar() or 0
        emit_alert(
            severity=Severity.P2,
            title="[copy] wallet pool transitions",
            body=(
                f"promoted: {len(decisions.promote)}\n"
                f"demoted: {len(decisions.demote)}\n"
                f"dropped: {len(decisions.drop)}\n"
                f"swap_in: {len(decisions.swap_in)}\n"
                f"active list: {n_active}/75"
            ),
            event_type="wallet_pool_transitions",
            bot_id="copy",
        )
    else:
        log.info("no_transitions")

    deleted = _truncate_event_log()
    log.info("event_log_truncated", rows=deleted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
