"""Lightweight per-service API-usage counters (Redis) → daily service-usage dashboard.

Every outbound Birdeye / Dexscreener request calls bump(service, status). We INCR a
per-day Redis counter (svc:{service}:calls:{YYYY-MM-DD}) plus a 429 counter on rate-limit,
so credit_pool_snapshot can roll them into service_usage_daily for Grafana. This is how we
SEE Birdeye/Dexscreener load + headroom (Helius is metered separately via helius:deliv).

FULLY FAIL-OPEN: a counter error must NEVER affect the API call it is measuring. Undercount
is acceptable (this is a directional usage gauge, not billing).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

_r = None
_TTL = 3888000  # 45 days — long enough for the dashboard's max range


def _client():
    global _r
    if _r is None:
        import redis
        url = os.environ.get(
            "REDIS_URL",
            f"redis://{os.environ.get('REDIS_HOST', 'redis')}:{os.environ.get('REDIS_PORT', '6379')}/0",
        )
        _r = redis.from_url(url, decode_responses=True)
    return _r


def bump(service: str, status: int | None = None) -> None:
    """Count one outbound call to `service`; also count a 429 if status == 429.
    Never raises."""
    try:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        r = _client()
        p = r.pipeline()
        ck = f"svc:{service}:calls:{day}"
        p.incr(ck)
        p.expire(ck, _TTL)
        if status == 429:
            ek = f"svc:{service}:e429:{day}"
            p.incr(ek)
            p.expire(ek, _TTL)
        p.execute()
    except Exception:
        pass
