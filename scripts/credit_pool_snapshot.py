"""Daily credit/pool snapshot -> audit_log.

Captures the data to build a credits-vs-wallets curve for sizing
copy_active_list_target against the Helius budget.

Why this works without the Helius usage API: Helius spend is ~99.7%
`webhookDelivery` (confirmed on the dashboard 2026-06-24), and each delivery is
one row in `wallet_events_log` (the webhook receiver logs every event). So the
24h event count is a reliable PROXY for daily credit burn — measured from our
own DB, no external call.

Calibrate once: compare a day's `deliveries_24h` here against that day's credits
on the Helius dashboard -> credits-per-delivery (~1). After that, the projected
monthly deliveries map straight to credits vs the 30M cap.

Writes audit_log event 'credit_pool_snapshot' with pool tier counts, subscribed
count (active + watch = what Helius bills), 24h deliveries, the per-subscribed
rate, a projection at active_list_target, and the top heavy-hitter wallets.

Usage:
  docker compose exec framework python -m scripts.credit_pool_snapshot
"""
from __future__ import annotations

import sys

from sqlalchemy import text

from framework.audit import write_audit
from framework.db import session_scope
from framework.logging_setup import configure_logging, get_logger

try:
    from framework.alerts import emit_alert
    from monitoring.alerting.taxonomy import Severity
    _ALERTS = True
except Exception:
    _ALERTS = False

log = get_logger("credit_pool_snapshot")

CREDIT_CAP = 30_000_000          # monthly cap (10M Developer + 20M autoscale, $149)
TARGET_FOR_PROJECTION = 300      # copy_active_list_target — what we're sizing toward


def main() -> int:
    configure_logging()

    with session_scope() as s:
        tiers = dict(
            s.execute(text(
                "SELECT tier, COUNT(*) FROM wallet_pool "
                "WHERE chain='solana' GROUP BY tier"
            )).all()
        )
        active = int(tiers.get("active", 0))
        watch = int(tiers.get("watch", 0))
        pruned = int(tiers.get("pruned", 0))
        subscribed = active + watch      # both tiers are webhook-subscribed = billed

        deliveries_24h = int(s.execute(text(
            "SELECT COUNT(*) FROM wallet_events_log "
            "WHERE event_at > now() - interval '24 hours'"
        )).scalar() or 0)

        # Split by which webhook delivered it (active vs watch tier) — shows how
        # much of the bill each tier drives.
        by_hook = dict(s.execute(text(
            "SELECT source_webhook, COUNT(*) FROM wallet_events_log "
            "WHERE event_at > now() - interval '24 hours' GROUP BY source_webhook"
        )).all())
        deliveries_active = int(by_hook.get("active", 0))
        deliveries_watch = int(by_hook.get("watch", 0))

        top = s.execute(text(
            "SELECT wallet_address AS w, COUNT(*) AS n FROM wallet_events_log "
            "WHERE event_at > now() - interval '24 hours' "
            "GROUP BY wallet_address ORDER BY n DESC LIMIT 10"
        )).all()
        top_list = [{"wallet": r.w, "deliveries_24h": int(r.n)} for r in top]

    per_sub = round(deliveries_24h / subscribed, 1) if subscribed else 0.0
    proj_monthly_at_target = int(per_sub * TARGET_FOR_PROJECTION * 30)
    pct_of_cap_at_target = round(100 * proj_monthly_at_target / CREDIT_CAP, 1)

    payload = {
        "active": active, "watch": watch, "pruned": pruned,
        "subscribed": subscribed,
        "deliveries_24h": deliveries_24h,
        "deliveries_active_24h": deliveries_active,
        "deliveries_watch_24h": deliveries_watch,
        "deliveries_per_subscribed_24h": per_sub,
        "projection_target_active": TARGET_FOR_PROJECTION,
        "projected_monthly_deliveries_at_target": proj_monthly_at_target,
        "projected_pct_of_30M_cap_at_target": pct_of_cap_at_target,
        "note": ("deliveries ~= Helius webhookDelivery credits (99.7% of spend); "
                 "proxy for daily burn, calibrate credits/delivery vs dashboard once"),
        "top_wallets_24h": top_list,
    }
    write_audit("credit_pool_snapshot", bot_id="copy", actor="cron", payload=payload)

    print(f"[credit_pool_snapshot] active={active} watch={watch} pruned={pruned} "
          f"subscribed={subscribed}")
    print(f"  deliveries_24h={deliveries_24h:,} (active={deliveries_active:,} "
          f"watch={deliveries_watch:,})  per_subscribed={per_sub}/day")
    print(f"  projected at {TARGET_FOR_PROJECTION} active: "
          f"{proj_monthly_at_target:,}/mo = {pct_of_cap_at_target}% of 30M cap")
    print("  top heavy-hitters (24h deliveries):")
    for t in top_list:
        print(f"    {t['wallet'][:16]}…  {t['deliveries_24h']:,}")

    if _ALERTS:
        try:
            emit_alert(
                severity=Severity.P2,
                title="[copy] credit/pool snapshot",
                body=(f"active {active} / watch {watch} (subscribed {subscribed})\n"
                      f"deliveries 24h: {deliveries_24h:,} (~Helius webhookDelivery credits)\n"
                      f"per subscribed wallet: {per_sub}/day\n"
                      f"projected at {TARGET_FOR_PROJECTION} active: "
                      f"{proj_monthly_at_target:,}/mo = {pct_of_cap_at_target}% of 30M cap"),
                bot_id="copy", event_type="credit_pool_snapshot",
                metadata={"subscribed": subscribed, "deliveries_24h": deliveries_24h},
            )
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
