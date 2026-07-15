"""Paid-promo SHADOW collector — Phase 2 of the info-source study (2026-07-11).

Gate-4 passed: buying runners at their Dexscreener paid-promo moment through the
cluster exit stack was net-positive (+$19.5-23.4k/33 adjacent events on $400
stakes, robust across stop/timeout configs) — but the corpus was RUNNERS-ONLY,
so it can't price the dud drag. This collector measures the missing number:
**P(run | promo)** and the true EV including every promoted token that goes nowhere.

SHADOW ONLY — records signals, NEVER trades (slow_cluster_detector pattern).

Each run (cron ~every 20 min):
  1. Poll Dexscreener `token-boosts/latest/v1` + `token-profiles/latest/v1`
     (free, 60rpm cap; ~13-17 solana tokens per 30-row snapshot).
  2. For each solana token not already recorded: fetch price via Birdeye
     (`/defi/price`) and insert a row (source=boost|profile, boost amount).
     First sighting only — later re-boosts of the same token are ignored.
  3. update_forward (piggybacked each run, cheap): for unresolved rows
     < RESOLVE_DAYS old and >= 1h since last refresh, refresh fwd_mult_max /
     fwd_mult_min from Birdeye 1H history since signal. Resolve at RESOLVE_DAYS.

Decide after ~2-4 weeks:
  SELECT source, count(*),
         avg((fwd_mult_max >= 2)::int) AS p_2x,
         avg((fwd_mult_max >= 4)::int) AS p_4x,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY fwd_mult_max) AS med_max
  FROM promo_shadow_signals GROUP BY 1;
plus a stack-sim replay over the recorded (token, signal ts, price) rows.

Runs IN-CONTAINER (framework: birdeye key + DB + repo). Log: ~/logs/promo_shadow.log
"""
from __future__ import annotations
import json
import os
import time
import urllib.request
import urllib.error

from sqlalchemy import text
from framework.db import session_scope
from framework.logging_setup import get_logger
from framework.api_usage import bump as _usage_bump

from bots.copy.config import get_copy_settings

log = get_logger("promo_shadow")
_KEY = get_copy_settings().birdeye_api_key
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0 Safari/537.36"

RESOLVE_DAYS = int(os.getenv("PS_RESOLVE_DAYS", "30"))
FWD_REFRESH_S = int(os.getenv("PS_FWD_REFRESH_S", "3600"))   # refresh forward at most hourly
MAX_FWD_PER_RUN = int(os.getenv("PS_MAX_FWD_PER_RUN", "60"))  # Birdeye budget guard
RATE = 0.25

DDL = """
CREATE TABLE IF NOT EXISTS promo_shadow_signals (
    token           VARCHAR(128) PRIMARY KEY,
    source          VARCHAR(16) NOT NULL,          -- boost | profile
    boost_amount    DOUBLE PRECISION,
    signal_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    signal_unix     BIGINT NOT NULL,
    price_at_signal DOUBLE PRECISION,
    fwd_mult_max    DOUBLE PRECISION,
    fwd_mult_min    DOUBLE PRECISION,
    resolved        BOOLEAN NOT NULL DEFAULT false,
    fwd_updated_at  TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_ps_resolved ON promo_shadow_signals (resolved, signal_at);
-- 2026-07-12 promo-CHARACTERISTIC columns (Dexscreener tokens/v1 at signal time) so the
-- ~08-01 P(run|promo) verdict can tell WHICH promos run (liq/mcap band, buy/sell, vol
-- accel = the practitioner filter stack). Additive, safe to re-run. KEEP THIS COMMENT
-- FREE OF THE SEMICOLON CHARACTER — the DDL executor splits statements on it.
ALTER TABLE promo_shadow_signals ADD COLUMN IF NOT EXISTS liquidity_usd DOUBLE PRECISION;
ALTER TABLE promo_shadow_signals ADD COLUMN IF NOT EXISTS market_cap DOUBLE PRECISION;
ALTER TABLE promo_shadow_signals ADD COLUMN IF NOT EXISTS liq_mcap_ratio DOUBLE PRECISION;
ALTER TABLE promo_shadow_signals ADD COLUMN IF NOT EXISTS buy_sell_ratio_h1 DOUBLE PRECISION;
ALTER TABLE promo_shadow_signals ADD COLUMN IF NOT EXISTS vol_accel DOUBLE PRECISION;
ALTER TABLE promo_shadow_signals ADD COLUMN IF NOT EXISTS age_hours DOUBLE PRECISION;
-- 2026-07-13 pump-MAGNITUDE (how big/extensive the paid promo is). boost_amount now holds
-- the cumulative all-time boost total (merged from the latest AND top boost feeds so a
-- profiled token that is ALSO boosted gets a size). boost_active = current active boosts.
-- KEEP THIS COMMENT FREE OF THE SEMICOLON CHARACTER.
ALTER TABLE promo_shadow_signals ADD COLUMN IF NOT EXISTS boost_active DOUBLE PRECISION;
-- 2026-07-14 SOURCE-OVERLAP. source is first-sighting only (boost precedence) so it CANNOT
-- show a token that is on BOTH feeds. These independent flags do (sticky-OR, refreshed each
-- run) so we can finally test profile+boost vs profile-only. Backfill infers old rows from
-- source/boost_amount (a pre-existing boost-source token that was ALSO profiled is unknowable
-- historically -> false). KEEP THESE COMMENTS FREE OF THE SEMICOLON CHARACTER.
ALTER TABLE promo_shadow_signals ADD COLUMN IF NOT EXISTS is_boosted BOOLEAN;
ALTER TABLE promo_shadow_signals ADD COLUMN IF NOT EXISTS is_profiled BOOLEAN;
UPDATE promo_shadow_signals SET is_boosted = (source = 'boost' OR boost_amount IS NOT NULL) WHERE is_boosted IS NULL;
UPDATE promo_shadow_signals SET is_profiled = (source = 'profile') WHERE is_profiled IS NULL;
-- 2026-07-14 FLIP TIMESTAMPS. When each promo type FIRST appeared for a token, so we can test
-- whether a 2nd promo (token on BOTH feeds) PRECEDES its run (front-runnable) or TRAILS it (just
-- momentum, we would be exit liquidity) = the leads-vs-lags question. Backfill uses `source`
-- (the flag true at first sighting is accurate = signal_at) the later-flipped flag stays NULL and
-- gets stamped forward by the re-sighting refresh. KEEP THESE COMMENTS SEMICOLON-FREE.
ALTER TABLE promo_shadow_signals ADD COLUMN IF NOT EXISTS first_boosted_at TIMESTAMPTZ;
ALTER TABLE promo_shadow_signals ADD COLUMN IF NOT EXISTS first_profiled_at TIMESTAMPTZ;
UPDATE promo_shadow_signals SET first_boosted_at = signal_at WHERE source = 'boost' AND first_boosted_at IS NULL;
UPDATE promo_shadow_signals SET first_profiled_at = signal_at WHERE source = 'profile' AND first_profiled_at IS NULL;
-- 2026-07-15 RE-ENTRY on a new promo wave. last_signal_at = when we last PUBLISHED a buy signal
-- for this token (first insert or a re-signal) resignal_count = how many re-signals fired (capped).
-- A known token whose boost total grows past the stored value gets ONE re-signal per cooldown up
-- to the cap so promobuy can re-enter a dipped-not-rugged token that is being freshly promoted.
-- KEEP THESE COMMENTS SEMICOLON-FREE.
ALTER TABLE promo_shadow_signals ADD COLUMN IF NOT EXISTS last_signal_at TIMESTAMPTZ;
ALTER TABLE promo_shadow_signals ADD COLUMN IF NOT EXISTS resignal_count INTEGER NOT NULL DEFAULT 0;
UPDATE promo_shadow_signals SET last_signal_at = signal_at WHERE last_signal_at IS NULL;
"""


def _http_json(url: str):
    r = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(r, timeout=20) as resp:
        out = json.loads(resp.read().decode())
    _usage_bump("dexscreener", 200)   # all _http_json calls in this collector are Dexscreener
    return out


def _dex(url: str, tries: int = 3):
    for i in range(tries):
        try:
            return _http_json(url)
        except urllib.error.HTTPError as e:
            _usage_bump("dexscreener", e.code)
            if e.code == 429:
                time.sleep(4 * (i + 1))
                continue
            log.warning("dex_http_error", code=e.code)
            return None
        except Exception as e:
            log.warning("dex_error", err=str(e))
            time.sleep(2)
    return None


def _be(path: str, tries: int = 3):
    for i in range(tries):
        try:
            r = urllib.request.Request(
                "https://public-api.birdeye.so" + path,
                headers={"X-API-KEY": _KEY, "x-chain": "solana",
                         "Accept": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(r, timeout=25) as resp:
                out = json.loads(resp.read().decode())
            _usage_bump("birdeye", 200)
            return out
        except urllib.error.HTTPError as e:
            _usage_bump("birdeye", e.code)
            if e.code == 429:
                time.sleep(4)
                continue
            return None
        except Exception:
            time.sleep(1.3)
    return None


def _token_pair(token: str) -> dict | None:
    """Sync Dexscreener /tokens/v1 snapshot (price+liq+mcap+practitioner features),
    parsed by the shared parse_dexscreener_pairs. Fail-open."""
    from bots.copy.venue.dex_quoter import parse_dexscreener_pairs
    try:
        d = _http_json(f"https://api.dexscreener.com/tokens/v1/solana/{token}")
        return parse_dexscreener_pairs(d)
    except Exception:
        return None


def _price_now(token: str) -> float | None:
    d = _be(f"/defi/price?address={token}")
    v = ((d or {}).get("data") or {}).get("value")
    return float(v) if v else None


def collect_new() -> int:
    """Poll boost + profile feeds. First sighting -> full insert (price snapshot + both
    source flags). A token seen again gets a lightweight FLAG refresh only (no price re-fetch,
    so the at-signal snapshot is preserved) -> captures a token profiled first and boosted
    LATER (or vice-versa), the 'both' case the old first-sighting-only recording missed."""
    # Fetch each feed once. The two boost feeds give pump MAGNITUDE (totalAmount = all-time
    # boost $, amount = active boosts); the profile feed is the paid-listing source.
    boost_latest = _dex("https://api.dexscreener.com/token-boosts/latest/v1") or []
    time.sleep(0.5)
    boost_top = _dex("https://api.dexscreener.com/token-boosts/top/v1") or []
    time.sleep(0.5)
    profiles = _dex("https://api.dexscreener.com/token-profiles/latest/v1") or []
    time.sleep(0.5)

    def _sol(rows):
        return {x.get("tokenAddress") for x in rows
                if x.get("chainId") == "solana" and x.get("tokenAddress")}
    boosts, profiled = _sol(boost_latest) | _sol(boost_top), _sol(profiles)
    all_toks = boosts | profiled
    if not all_toks:
        return 0

    # Pump-size map (largest cumulative boost total + current active count) from both boost feeds.
    boost_map: dict[str, tuple[float | None, float | None]] = {}
    for x in (boost_latest + boost_top):
        if x.get("chainId") != "solana":
            continue
        tok = x.get("tokenAddress")
        if not tok:
            continue
        ta, am = x.get("totalAmount"), x.get("amount")
        prev = boost_map.get(tok)
        if prev is None or (ta or 0) > (prev[0] or 0):
            boost_map[tok] = (ta, am)

    with session_scope() as s:
        known = {r[0] for r in s.execute(text(
            "SELECT token FROM promo_shadow_signals WHERE token = ANY(:toks)"
        ), {"toks": list(all_toks)}).fetchall()}

    now = int(time.time())
    rpub = _redis_pub()
    cs = get_copy_settings()
    re_enabled, re_cd, re_max = (cs.copy_promobuy_reentry_enabled,
                                 cs.copy_promobuy_reentry_cooldown_minutes,
                                 cs.copy_promobuy_reentry_max)

    # KNOWN tokens seen again: (a) RE-SIGNAL a re-entry if a NEW boost appeared since we last
    # signaled (churn-guarded by cooldown + per-token cap), then (b) sticky-OR the flags + keep
    # the largest boost total / latest active.
    updated = resignaled = 0
    reseen = all_toks & known
    if reseen:
        with session_scope() as s:
            for tok in reseen:
                amt, active = boost_map.get(tok, (None, None))
                # (a) re-signal — atomic: only stamps + returns a row when the incoming boost
                # exceeds the STORED total (a fresh boost) AND the cooldown has passed AND we are
                # under the per-token cap. Runs BEFORE the boost update so :amt compares vs the OLD
                # boost. The bot still applies liquidity + its own re-entry cooldown.
                if re_enabled and rpub is not None and amt is not None:
                    r_row = s.execute(text(
                        "UPDATE promo_shadow_signals SET last_signal_at = now(), "
                        "resignal_count = COALESCE(resignal_count, 0) + 1 WHERE token = :t "
                        "AND :amt > COALESCE(boost_amount, 0) "
                        "AND (last_signal_at IS NULL OR last_signal_at < now() - (:cd * interval '1 minute')) "
                        "AND COALESCE(resignal_count, 0) < :mx RETURNING price_at_signal"
                    ), {"t": tok, "amt": amt, "cd": re_cd, "mx": re_max}).fetchone()
                    if r_row is not None:
                        try:
                            rpub.publish("copy:promo_signals", json.dumps(
                                {"token": tok, "source": ("boost" if tok in boosts else "profile"),
                                 "is_boosted": tok in boosts, "is_profiled": tok in profiled,
                                 "boost_amount": amt, "boost_active": active,
                                 "price": r_row[0], "ts": now, "reentry": True}))
                            resignaled += 1
                            log.info("promo_resignal", token=tok[:12], boost=amt)
                        except Exception:
                            log.warning("promo_resignal_publish_failed", token=tok[:12])
                # (b) flag/boost refresh
                s.execute(text(
                    "UPDATE promo_shadow_signals SET "
                    "is_boosted = COALESCE(is_boosted, false) OR :ib, "
                    "is_profiled = COALESCE(is_profiled, false) OR :ip, "
                    "first_boosted_at = CASE WHEN :ib AND first_boosted_at IS NULL THEN now() ELSE first_boosted_at END, "
                    "first_profiled_at = CASE WHEN :ip AND first_profiled_at IS NULL THEN now() ELSE first_profiled_at END, "
                    "boost_amount = CASE WHEN :amt IS NOT NULL "
                    "  THEN GREATEST(COALESCE(boost_amount, 0), :amt) ELSE boost_amount END, "
                    "boost_active = COALESCE(:bact, boost_active), updated_at = now() "
                    "WHERE token = :t"
                ), {"ib": tok in boosts, "ip": tok in profiled,
                    "amt": amt, "bact": active, "t": tok})
                updated += 1

    # FRESH tokens: full insert with price snapshot + both flags.
    fresh = all_toks - known
    inserted = 0
    for tok in fresh:
        is_b, is_p = tok in boosts, tok in profiled
        source = "boost" if is_b else "profile"      # first-seen label (kept for continuity)
        amount, active = boost_map.get(tok, (None, None))
        pair = _token_pair(tok)                      # one Dexscreener call: price+liq+mcap+features
        px = (pair or {}).get("price_usd") or _price_now(tok)
        time.sleep(RATE)
        with session_scope() as s:
            s.execute(text(
                "INSERT INTO promo_shadow_signals "
                "(token, source, is_boosted, is_profiled, first_boosted_at, first_profiled_at, "
                " last_signal_at, boost_amount, boost_active, signal_unix, price_at_signal, "
                " liquidity_usd, market_cap, liq_mcap_ratio, buy_sell_ratio_h1, vol_accel, age_hours) "
                "VALUES (:t, :src, :ib, :ip, "
                " CASE WHEN :ib THEN now() ELSE NULL END, CASE WHEN :ip THEN now() ELSE NULL END, "
                " now(), :amt, :bact, :u, :px, :liq, :mc, :lmr, :bsr, :va, :age) "
                "ON CONFLICT (token) DO NOTHING"
            ), {"t": tok, "src": source, "ib": is_b, "ip": is_p, "amt": amount,
                "bact": active, "u": now, "px": px,
                "liq": (pair or {}).get("liquidity_usd"),
                "mc": (pair or {}).get("market_cap"),
                "lmr": (pair or {}).get("liq_mcap_ratio"),
                "bsr": (pair or {}).get("buy_sell_ratio_h1"),
                "va": (pair or {}).get("vol_accel"),
                "age": (pair or {}).get("age_hours")})
        inserted += 1
        log.info("promo_signal", token=tok[:12], source=source,
                 both=(is_b and is_p), price=px, boost=amount)
        # Feed the live promobuy strategy (fail-open; no-op if nobody subscribes).
        if rpub is not None:
            try:
                rpub.publish("copy:promo_signals", json.dumps(
                    {"token": tok, "source": source, "is_boosted": is_b, "is_profiled": is_p,
                     "boost_amount": amount, "boost_active": active, "price": px, "ts": now}))
            except Exception:
                log.warning("promo_publish_failed", token=tok[:12])
    if updated:
        log.info("promo_flags_refreshed", n=updated)
    return inserted


def _redis_pub():
    """Sync Redis client for publishing promo signals to the bot. Fail-open."""
    import os
    try:
        import redis
        url = os.environ.get(
            "REDIS_URL",
            f"redis://{os.environ.get('REDIS_HOST', 'redis')}:{os.environ.get('REDIS_PORT', '6379')}/0")
        return redis.from_url(url, decode_responses=True)
    except Exception:
        return None


def update_forward() -> int:
    now = int(time.time())
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT token, signal_unix, price_at_signal FROM promo_shadow_signals "
            "WHERE NOT resolved AND price_at_signal IS NOT NULL "
            "AND (fwd_updated_at IS NULL OR fwd_updated_at < now() - make_interval(secs => :ref)) "
            "ORDER BY fwd_updated_at ASC NULLS FIRST LIMIT :lim"
        ), {"ref": FWD_REFRESH_S, "lim": MAX_FWD_PER_RUN}).fetchall()

    updated = 0
    for token, sig_unix, px0 in rows:
        h = _be(f"/defi/history_price?address={token}&address_type=token"
                f"&type=1H&time_from={sig_unix}&time_to={now}")
        time.sleep(RATE)
        items = ((h or {}).get("data") or {}).get("items") or []
        vals = [float(it["value"]) for it in items if it.get("value")]
        resolved = (now - sig_unix) > RESOLVE_DAYS * 86400
        if vals and px0:
            fmax = max(vals) / px0
            fmin = min(vals) / px0
            with session_scope() as s:
                s.execute(text(
                    "UPDATE promo_shadow_signals SET fwd_mult_max=:mx, fwd_mult_min=:mn, "
                    "resolved=:res, fwd_updated_at=now(), updated_at=now() WHERE token=:t"
                ), {"mx": fmax, "mn": fmin, "res": resolved, "t": token})
            updated += 1
        elif resolved:
            with session_scope() as s:
                s.execute(text(
                    "UPDATE promo_shadow_signals SET resolved=true, fwd_updated_at=now(), "
                    "updated_at=now() WHERE token=:t"), {"t": token})
    return updated


def main() -> int:
    with session_scope() as s:
        for stmt in DDL.split(";"):
            if stmt.strip():
                s.execute(text(stmt))
    inserted = collect_new()
    updated = update_forward()
    with session_scope() as s:
        n, sig = s.execute(text(
            "SELECT count(*), count(*) FILTER (WHERE fwd_mult_max >= 2) "
            "FROM promo_shadow_signals")).fetchone()
    log.info("promo_shadow_done", inserted=inserted, fwd_updated=updated,
             total=n, reached_2x=sig)
    print(f"[promo_shadow] +{inserted} new, {updated} fwd-updated, "
          f"total {n} (>=2x so far: {sig})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
