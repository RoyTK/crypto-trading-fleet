"""Daily credit/pool snapshot -> audit_log.

Reports TRUE Helius credit burn vs the plan, so we can deliberately run the
active pool up toward the 10M plan (Roy 2026-07-10: use the whole plan, a little
autoscale is fine — don't leave prepaid credits on the table).

CREDIT SOURCE (fixed 2026-07-10): Helius bills per webhook DELIVERY (~1 credit
each), and ~100% of spend is webhookDelivery. Earlier this script counted rows in
`wallet_events_log` as a delivery proxy — but the receiver only logs MATCHED buys
(~6% of deliveries), so it under-reported burn ~17x (said ~2% of cap while the
dashboard showed ~70% of the 10M plan). The receiver now tallies EVERY delivery
into per-tier daily Redis counters (`helius:deliv:{YYYY-MM-DD}:{tier}`); this reads
those for the real number. `wallet_events_log` is kept only as a "signal volume"
line (matched buys), NOT as the credit measure.

Plan = 10M credits/mo; autoscaling = separate PAID 20M bucket (30M ceiling). We
gauge vs the 10M plan (primary) and flag autoscale territory above it.

Usage:
  docker compose exec framework python -m scripts.credit_pool_snapshot
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

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

PLAN_CREDITS = 10_000_000        # monthly Developer plan (prepaid — the target to fill)
AUTOSCALE_CREDITS = 20_000_000   # optional PAID overage bucket above the plan
BILLED_TIERS = ("active", "watch", "teamfollow")  # each has its own subscribed webhook
DELIV_PREFIX = "helius:deliv:"   # receiver key: helius:deliv:{YYYY-MM-DD}:{tier}
LOOKBACK_DAYS = 7                # trailing full days to average the daily rate over
USAGE_BACKFILL_DAYS = 45         # how far back to (re)snapshot service_usage_daily each run

SERVICE_USAGE_DDL = """
CREATE TABLE IF NOT EXISTS service_usage_daily (
    day     DATE NOT NULL,
    service VARCHAR(24) NOT NULL,       -- helius | birdeye | dexscreener
    tier    VARCHAR(24) NOT NULL DEFAULT 'all',
    calls   BIGINT NOT NULL DEFAULT 0,  -- deliveries (helius) or API calls (birdeye/dex)
    errors  BIGINT NOT NULL DEFAULT 0,  -- 429 rate-limit hits
    PRIMARY KEY (day, service, tier)
)
"""

# Birdeye bills COMPUTE UNITS (CU), not calls. Our svc:birdeye:calls counter is a poor CU
# proxy for two reasons: (1) scrape_runners + slow_cluster hit history_price via their own
# un-instrumented helpers (invisible to the counter), and (2) history_price is Birdeye
# "Dynamic CU" — its cost varies by the time range requested, so calls×weight can't be right.
# So we DON'T estimate: we poll Birdeye's own usage endpoint (/utils/v1/credits — the exact
# source the Birdeye portal shows) and snapshot the real number.
BIRDEYE_CREDITS_DDL = """
CREATE TABLE IF NOT EXISTS birdeye_credits (
    snapshot_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    cycle_start   TIMESTAMPTZ,
    cycle_end     TIMESTAMPTZ,
    cu_used       BIGINT NOT NULL,
    cu_remaining  BIGINT NOT NULL,
    cu_limit      BIGINT NOT NULL,
    overage_cu    BIGINT NOT NULL DEFAULT 0,
    overage_cost  DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_at)
)
"""


def _redis_client():
    import redis  # sync client (framework container)
    url = os.environ.get(
        "REDIS_URL",
        f"redis://{os.environ.get('REDIS_HOST', 'redis')}:{os.environ.get('REDIS_PORT', '6379')}/0",
    )
    return redis.from_url(url, decode_responses=True)


def _read_deliveries(r) -> tuple[dict, dict]:
    """Return (per_day_totals, yesterday_by_tier) from the receiver's Redis counters.

    per_day_totals: {date_str: total_deliveries} for the last LOOKBACK_DAYS full UTC days.
    yesterday_by_tier: {tier: deliveries} for the most recent complete UTC day.
    """
    today = datetime.now(timezone.utc).date()
    per_day: dict[str, int] = {}
    yesterday_by_tier: dict[str, int] = {}
    for i in range(1, LOOKBACK_DAYS + 1):  # 1 = yesterday (last complete day)
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        day_total = 0
        for tier in BILLED_TIERS:
            try:
                v = int(r.get(f"{DELIV_PREFIX}{d}:{tier}") or 0)
            except Exception:
                v = 0
            day_total += v
            if i == 1:
                yesterday_by_tier[tier] = v
        if day_total:
            per_day[d] = day_total
    return per_day, yesterday_by_tier


def _snapshot_service_usage(r) -> int:
    """Roll the Redis usage counters into service_usage_daily (the Grafana source for the
    Service Usage dashboard): Helius deliveries per tier (helius:deliv:*) + Birdeye /
    Dexscreener API calls & 429s (svc:*). Idempotent upsert over a trailing window, so it
    self-backfills Helius history and keeps every service current. Fail-open."""
    today = datetime.now(timezone.utc).date()
    rows: list[tuple] = []
    for i in range(0, USAGE_BACKFILL_DAYS + 1):
        d = today - timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        for tier in BILLED_TIERS:  # Helius: one row per subscribed webhook tier
            try:
                v = int(r.get(f"{DELIV_PREFIX}{ds}:{tier}") or 0)
            except Exception:
                v = 0
            if v:
                rows.append((d, "helius", tier, v, 0))
        for svc in ("birdeye", "dexscreener"):  # our API calls + rate-limit hits
            try:
                calls = int(r.get(f"svc:{svc}:calls:{ds}") or 0)
                e429 = int(r.get(f"svc:{svc}:e429:{ds}") or 0)
            except Exception:
                calls = e429 = 0
            if calls or e429:
                rows.append((d, svc, "all", calls, e429))
    if not rows:
        return 0
    with session_scope() as s:
        s.execute(text(SERVICE_USAGE_DDL))
        for (d, svc, tier, calls, errors) in rows:
            s.execute(text(
                "INSERT INTO service_usage_daily (day, service, tier, calls, errors) "
                "VALUES (:d,:svc,:t,:c,:e) ON CONFLICT (day, service, tier) "
                "DO UPDATE SET calls=EXCLUDED.calls, errors=EXCLUDED.errors"
            ), {"d": d, "svc": svc, "t": tier, "c": calls, "e": errors})
    return len(rows)


def _snapshot_birdeye_credits() -> dict | None:
    """Poll Birdeye's own /utils/v1/credits (the exact source the portal shows) and record a
    snapshot of REAL compute-unit usage into birdeye_credits. Returns the parsed summary (with
    a linear end-of-cycle projection) or None. Fail-open — never raises."""
    import json
    import urllib.request
    try:
        from bots.copy.config import get_copy_settings
        key = get_copy_settings().birdeye_api_key
        if not key:
            return None
        req = urllib.request.Request(
            "https://public-api.birdeye.so/utils/v1/credits",
            headers={"X-API-KEY": key, "x-chain": "solana", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            d = (json.loads(resp.read().decode()) or {}).get("data") or {}
        used = int((d.get("usage") or {}).get("total") or 0)
        remaining = int((d.get("remaining") or {}).get("total") or 0)
        over_cu = int((d.get("overage_usage") or {}).get("total") or 0)
        over_cost = float((d.get("overage_cost") or {}).get("total") or 0.0)
        limit = used + remaining
        cs, ce = d.get("start_time"), d.get("end_time")
        cstart = datetime.fromtimestamp(cs, tz=timezone.utc) if cs else None
        cend = datetime.fromtimestamp(ce, tz=timezone.utc) if ce else None
        with session_scope() as s:
            s.execute(text(BIRDEYE_CREDITS_DDL))
            s.execute(text(
                "INSERT INTO birdeye_credits (snapshot_at, cycle_start, cycle_end, cu_used, "
                "cu_remaining, cu_limit, overage_cu, overage_cost) "
                "VALUES (now(), :cs, :ce, :used, :rem, :lim, :ocu, :ocost)"
            ), {"cs": cstart, "ce": cend, "used": used, "rem": remaining,
                "lim": limit, "ocu": over_cu, "ocost": over_cost})
        # linear projection to cycle end
        projected = used
        if cs and ce and ce > cs:
            elapsed = max(1e-9, (datetime.now(timezone.utc).timestamp() - cs) / (ce - cs))
            projected = int(used / min(1.0, elapsed))
        return {"cu_used": used, "cu_remaining": remaining, "cu_limit": limit,
                "pct_of_limit": round(100 * used / limit, 1) if limit else 0.0,
                "overage_cu": over_cu, "overage_cost": over_cost,
                "projected_cycle_cu": projected, "cycle_end": ce}
    except Exception as e:
        log.warning("birdeye_credits_snapshot_failed", err=str(e))
        return None


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
        teamfollow = int(tiers.get("teamfollow", 0))
        pruned = int(tiers.get("pruned", 0))
        subscribed = active + watch + teamfollow  # all three are webhook-billed

        # Secondary "signal volume" line only (matched buys ≈ 6% of deliveries) —
        # NOT the credit measure.
        matched_buys_24h = int(s.execute(text(
            "SELECT COUNT(*) FROM wallet_events_log "
            "WHERE event_at > now() - interval '24 hours'"
        )).scalar() or 0)

    # ---- Real burn from the receiver's delivery counters ----
    warming_up = False
    try:
        r = _redis_client()
        per_day, yday_by_tier = _read_deliveries(r)
        try:
            _snapshot_service_usage(r)
        except Exception as e:
            log.warning("service_usage_snapshot_failed", err=str(e))
    except Exception as e:
        log.warning("delivery_counter_read_failed", err=str(e))
        per_day, yday_by_tier = {}, {}

    # ---- Real Birdeye compute-unit burn (polled from Birdeye, not estimated) ----
    be_cu = _snapshot_birdeye_credits()

    if per_day:
        daily_avg = int(round(sum(per_day.values()) / len(per_day)))
        yesterday_total = sum(yday_by_tier.values())
    else:
        # Counters not populated yet (first ~1 day after deploy) — be honest.
        warming_up = True
        daily_avg = 0
        yesterday_total = 0

    projected_monthly = daily_avg * 30
    pct_of_plan = round(100 * projected_monthly / PLAN_CREDITS, 1)
    over_plan = max(0, projected_monthly - PLAN_CREDITS)
    pct_of_autoscale = round(100 * over_plan / AUTOSCALE_CREDITS, 1) if over_plan else 0.0

    payload = {
        "active": active, "watch": watch, "teamfollow": teamfollow, "pruned": pruned,
        "subscribed": subscribed,
        "deliveries_yesterday": yesterday_total,
        "deliveries_yesterday_by_tier": yday_by_tier,
        "deliveries_daily_avg_7d": daily_avg,
        "days_with_data": len(per_day),
        "projected_monthly_credits": projected_monthly,
        "plan_credits": PLAN_CREDITS,
        "pct_of_10M_plan": pct_of_plan,
        "projected_autoscale_credits": over_plan,
        "pct_of_20M_autoscale": pct_of_autoscale,
        "matched_buys_24h_signal_volume": matched_buys_24h,
        "warming_up": warming_up,
        "birdeye_cu": be_cu,
        "note": ("real burn = per-tier delivery counters (helius:deliv:*); "
                 "~1 credit/delivery; matched_buys is signal volume only; "
                 "birdeye_cu = REAL compute units from /utils/v1/credits (not call-count)"),
    }
    write_audit("credit_pool_snapshot", bot_id="copy", actor="cron", payload=payload)

    print(f"[credit_pool_snapshot] active={active} watch={watch} "
          f"teamfollow={teamfollow} pruned={pruned} subscribed={subscribed}")
    if warming_up:
        print("  ⏳ delivery counters warming up (need 1 full UTC day post-deploy); "
              "true burn is on the Helius dashboard until then.")
    else:
        yt = yday_by_tier
        print(f"  deliveries yesterday: {yesterday_total:,} "
              f"(active={yt.get('active',0):,} teamfollow={yt.get('teamfollow',0):,} "
              f"watch={yt.get('watch',0):,})")
        print(f"  daily avg ({len(per_day)}d): {daily_avg:,}/day")
        print(f"  projected: {projected_monthly:,}/mo = {pct_of_plan}% of the 10M plan"
              + (f"  → +{over_plan:,} into paid autoscale ({pct_of_autoscale}% of 20M)"
                 if over_plan else "  (no autoscale spend)"))
    print(f"  [signal volume] matched buys 24h: {matched_buys_24h:,}")
    if be_cu:
        print(f"  [birdeye CU] {be_cu['cu_used']:,}/{be_cu['cu_limit']:,} used "
              f"({be_cu['pct_of_limit']}%), {be_cu['cu_remaining']:,} left; "
              f"projected end-of-cycle {be_cu['projected_cycle_cu']:,}"
              + (f"  ⚠ overage +{be_cu['overage_cu']:,} (${be_cu['overage_cost']:.2f})"
                 if be_cu['overage_cu'] else ""))
    else:
        print("  [birdeye CU] poll unavailable (see logs)")

    if _ALERTS:
        try:
            if warming_up:
                body = (f"active {active} / watch {watch} / teamfollow {teamfollow} "
                        f"(subscribed {subscribed})\n"
                        "delivery counters warming up — real burn on the Helius dashboard")
            else:
                body = (f"active {active} / watch {watch} / teamfollow {teamfollow} "
                        f"(subscribed {subscribed})\n"
                        f"real deliveries yesterday: {yesterday_total:,}\n"
                        f"projected: {projected_monthly:,}/mo = {pct_of_plan}% of the 10M plan"
                        + (f" (+{over_plan:,} paid autoscale)" if over_plan else ""))
            if be_cu:
                body += (f"\nBirdeye CU: {be_cu['cu_used']:,}/{be_cu['cu_limit']:,} "
                         f"({be_cu['pct_of_limit']}%), proj end-of-cycle {be_cu['projected_cycle_cu']:,}"
                         + (f" — overage +{be_cu['overage_cu']:,} (${be_cu['overage_cost']:.2f})"
                            if be_cu['overage_cu'] else ""))
            emit_alert(
                severity=Severity.P2,
                title="[copy] credit/pool snapshot",
                body=body,
                bot_id="copy", event_type="credit_pool_snapshot",
                metadata={"subscribed": subscribed,
                          "projected_monthly_credits": projected_monthly,
                          "pct_of_10M_plan": pct_of_plan},
            )
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
