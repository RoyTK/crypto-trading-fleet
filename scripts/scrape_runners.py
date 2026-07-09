#!/usr/bin/env python3
"""Server-side runner-scraper cron: fresh runners -> pre-run accumulators.

Runs IN-CONTAINER (bot_copy: has settings.birdeye_api_key + DB + the repo on
/app/scripts). Forward-looking discovery — catches tokens AS they run, so it
needs NO Dune historical SQL; uses the idle Birdeye budget (free tier, 1 RPS).

Pipeline (all validated 2026-07-01, see project_cluster_database_build memory):
  1. DISCOVERY = GeckoTerminal trending/new/top pools (browser User-Agent — the
     urllib default UA trips Cloudflare 403). Recent movers, any token age.
  2. RUN-START = Birdeye history_price (token-level, spans pools — single-pool
     OHLCV misses the pump.fun->Raydium migration). run_start = last pre-peak
     daily point where price <= PEAK_FRACTION of the peak.
  3. PRE-RUN ACCUMULATORS = Birdeye txs/token/seek_by_time paged back through the
     QUIET window [run_start - PRERUN_DAYS, run_start]. A quiet window is cheap
     AND naturally isolates accumulators (no momentum crowd yet). Per owner:
     n_buys, bought_usd, first_buy, lead_days = run_start - first_buy.
  4. Cross-reference wallet_pool (which candidates are already known / pruned).
  5. Persist to prerun_accumulators (self-bootstrapping table) + print a
     recurrence report (wallets appearing across >= MIN_RECUR runners).

HONEST GUARDRAIL (proven this session): raw recurrence is a SPRAY FARM — only a
thin validated subset is followable. This cron STAGES candidates for vetting /
forward-test; it does NOT auto-promote anyone to an active roster.

Ops: run throttled from the Hetzner IP (NOT Roy's residential IP — Cloudflare
WAF). Rate-limited to ~1 Birdeye call/s (free tier). A full daily pass over
MAX_TOKENS ~= a few minutes.

Run:  docker compose exec -T bot_copy python /app/scripts/scrape_runners.py
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from bots.copy.config import get_copy_settings
from framework.db import session_scope
from framework.logging_setup import get_logger

log = get_logger(__name__)

# --- knobs (env-overridable so cadence/thresholds tune without a redeploy) ---
LOOKBACK_DAYS = int(os.getenv("SCRAPE_LOOKBACK_DAYS", "45"))       # run-start search horizon
PRERUN_DAYS = int(os.getenv("SCRAPE_PRERUN_DAYS", "14"))          # accumulation window before run_start
MIN_LIQ_USD = float(os.getenv("SCRAPE_MIN_LIQ_USD", "30000"))    # discovery liquidity floor
MIN_VOL24_USD = float(os.getenv("SCRAPE_MIN_VOL24_USD", "100000"))  # discovery 24h volume floor
DISC_PAGES = int(os.getenv("SCRAPE_DISC_PAGES", "3"))          # GeckoTerminal pages per feed (breadth)
MIN_RUN_X = float(os.getenv("SCRAPE_MIN_RUN_X", "3.0"))          # peak/base to count as a run
PEAK_FRACTION = float(os.getenv("SCRAPE_PEAK_FRACTION", "0.12"))  # run_start = last pre-peak pt <= this*peak
DUST_USD = float(os.getenv("SCRAPE_DUST_USD", "50"))             # ignore sub-dust buy legs
MIN_PRERUN_USD = float(os.getenv("SCRAPE_MIN_PRERUN_USD", "200"))  # accumulator commitment floor
MAX_TOKENS = int(os.getenv("SCRAPE_MAX_TOKENS", "40"))          # throttle: tokens processed per run
# seek_by_time paging cap per token. Busy tokens have thousands of trades in the
# dense day before breakout; the cap bounds cost. When exhausted we log
# prerun_window_truncated with the oldest day reached — coverage is honest, and
# the immediate pre-run (where the corpus showed 78% of accumulation lives) is
# captured either way. The rare multi-day tail is knowingly under-sampled here.
MAX_PRERUN_PAGES = int(os.getenv("SCRAPE_MAX_PRERUN_PAGES", "40"))  # ~2000 trades/token
MIN_RECUR = int(os.getenv("SCRAPE_MIN_RECUR", "3"))            # recurrence report threshold
RECUR_WINDOW_DAYS = int(os.getenv("SCRAPE_RECUR_WINDOW_DAYS", "90"))
# Bot-frequency filter (2026-07-05): recurrence surfaces BOTS — a wallet that buys
# everything is "pre-run" on every runner by pure coverage, so it always tops the
# recurrence list. Forward-validation proved every high-recurrence candidate was a
# 200-100,000 trades/day spray/router/MEV bot with NO follow-edge. A genuine
# deliberate accumulator is LOW-frequency + selective. So we measure each candidate's
# trades/day and filter bots out of the promotable list.
BOT_FREQ_PER_DAY = float(os.getenv("SCRAPE_BOT_FREQ_PER_DAY", "50"))  # > this = automated bot
ACTIVITY_TTL_DAYS = int(os.getenv("SCRAPE_ACTIVITY_TTL_DAYS", "7"))   # re-measure only if staler
RATE_SLEEP = float(os.getenv("SCRAPE_RATE_SLEEP", "1.1"))       # Birdeye free tier ~1 RPS
GT_SLEEP = float(os.getenv("SCRAPE_GT_SLEEP", "5.0"))          # GeckoTerminal free tier throttles bursts (429 at ~27/min); ~12/min stays clear
# Skip a token whose run we already captured — re-scanning a past run returns
# byte-identical data (window is fixed in the past). A GENUINELY NEW run gets a
# new run_date > TOLERANCE days from the old one and passes through ("skip after
# the run unless it runs again"). This keeps each night's Birdeye budget on
# net-new runners; most dupes also age out of "trending" within ~24h anyway.
RESCAN_TOLERANCE_DAYS = int(os.getenv("SCRAPE_RESCAN_TOLERANCE_DAYS", "5"))

# Birdeye call counter — logged per run so we can MEASURE real usage against the
# free-tier budget (30k CU/mo) instead of guessing. Reset at main() start.
_BE_CALLS = 0

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
QUOTES = {  # priced quote mints — used to value the other leg in USD
    "So11111111111111111111111111111111111111112",  # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

_KEY = get_copy_settings().birdeye_api_key


# ----------------------------- HTTP helpers ---------------------------------
def _gt(path: str) -> dict:
    r = urllib.request.Request(
        "https://api.geckoterminal.com/api/v2" + path,
        headers={"Accept": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(r, timeout=25) as resp:
        return json.loads(resp.read().decode())


def _gt_retry(path: str, tries: int = 4) -> dict | None:
    """GeckoTerminal with 429 backoff. The free tier throttles bursts even at ~12/min
    (GT_SLEEP=5 wasn't enough), so on 429 wait escalating 8/16/24s and retry rather than
    dropping the feed page — recovers the full trending+new+top sweep."""
    for i in range(tries):
        try:
            return _gt(path)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(8 * (i + 1))
                continue
            log.warning("gt_http_error", code=e.code, path=path)
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("gt_exception", err=str(e), path=path)
            time.sleep(3)
    return None


def _be(path: str) -> dict:
    global _BE_CALLS
    _BE_CALLS += 1  # count every HTTP hit (incl. retries) for budget measurement
    r = urllib.request.Request(
        "https://public-api.birdeye.so" + path,
        headers={"X-API-KEY": _KEY, "x-chain": "solana",
                 "Accept": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(r, timeout=25) as resp:
        return json.loads(resp.read().decode())


def _be_retry(path: str, tries: int = 3) -> dict | None:
    for i in range(tries):
        try:
            return _be(path)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5)
                continue
            log.warning("birdeye_http_error", code=e.code, path=path.split("?")[0])
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("birdeye_exception", err=str(e), path=path.split("?")[0])
            time.sleep(2)
    return None


# ----------------------------- 1. discovery ---------------------------------
def discover_runners() -> list[dict]:
    """GeckoTerminal trending/new/top pools across DISC_PAGES pages each -> deduped
    base mints past liq/vol floors. Broader = more distinct recent runners, so genuine
    low-frequency accumulators can recur across enough tokens to clear the bot filter
    (a thin corpus only lets high-coverage bots recur)."""
    seen: dict[str, dict] = {}
    for feed in ("trending_pools", "new_pools", "pools"):
        for page in range(1, DISC_PAGES + 1):
            d = _gt_retry(f"/networks/solana/{feed}?page={page}")
            if d is None:
                log.warning("gt_feed_failed", feed=feed, page=page)
                continue
            data = d.get("data", [])
            if not data:
                break  # no more pages on this feed
            for p in data:
                a = p.get("attributes", {})
                rel = p.get("relationships", {}).get("base_token", {}).get("data", {})
                mint = (rel.get("id") or "").replace("solana_", "")
                if not mint:
                    continue
                liq = float(a.get("reserve_in_usd") or 0)
                v24 = float((a.get("volume_usd") or {}).get("h24") or 0)
                if liq < MIN_LIQ_USD or v24 < MIN_VOL24_USD:
                    continue
                prev = seen.get(mint)
                if prev is None or v24 > prev["v24"]:
                    seen[mint] = {"mint": mint, "symbol": (a.get("name") or "").split(" /")[0],
                                  "liq": liq, "v24": v24}
            time.sleep(GT_SLEEP)
    return sorted(seen.values(), key=lambda x: x["v24"], reverse=True)


# ----------------------------- 2. run-start ---------------------------------
def detect_run_start(mint: str) -> dict | None:
    """Token-level daily history -> (run_start_ts, peak_ts, peak_px, run_x)."""
    now = int(time.time())
    h = _be_retry(f"/defi/history_price?address={mint}&address_type=token"
                  f"&type=1D&time_from={now - LOOKBACK_DAYS * 86400}&time_to={now}")
    time.sleep(RATE_SLEEP)
    items = ((h or {}).get("data") or {}).get("items") or []
    pts = [(it["unixTime"], float(it["value"])) for it in items if it.get("value")]
    if len(pts) < 3:
        return None
    pts.sort(key=lambda x: x[0])
    peak_ts, peak_px = max(pts, key=lambda x: x[1])
    pre = [p for p in pts if p[0] <= peak_ts]
    if len(pre) < 2 or peak_px <= 0:
        return None
    base_px = min(p[1] for p in pre)
    if base_px <= 0:
        return None
    run_x = peak_px / base_px
    if run_x < MIN_RUN_X:
        return None
    # run_start = LATEST pre-peak point still under PEAK_FRACTION of the peak
    thresh = PEAK_FRACTION * peak_px
    run_start_ts = pre[0][0]
    for ts, px in pre:
        if px <= thresh:
            run_start_ts = ts
    if run_start_ts >= peak_ts:  # broke out immediately — no quiet pre-run window
        return None
    return {"run_start_ts": run_start_ts, "peak_ts": peak_ts,
            "peak_px": peak_px, "base_px": base_px, "run_x": run_x}


# ------------------------- 3. pre-run accumulators --------------------------
def _leg_usd(item: dict) -> float:
    """USD value of a swap: prefer the priced quote leg, fall back to base*tokenPrice."""
    q = item.get("quote") or {}
    if q.get("address") in QUOTES and q.get("uiAmount") and q.get("price"):
        return abs(float(q["uiAmount"])) * float(q["price"])
    b = item.get("base") or {}
    tp = item.get("tokenPrice")
    if b.get("uiAmount") and tp:
        return abs(float(b["uiAmount"])) * float(tp)
    return 0.0


def prerun_accumulators(mint: str, run_start_ts: int) -> dict[str, dict]:
    """Page seek_by_time back through [run_start - PRERUN_DAYS, run_start];
    aggregate BUY legs per owner. Returns wallet -> {n_buys, bought_usd, first_buy_ts}."""
    window_start = run_start_ts - PRERUN_DAYS * 86400
    before = run_start_ts
    agg: dict[str, dict] = {}
    oldest = run_start_ts
    reached = False
    for _ in range(MAX_PRERUN_PAGES):
        t = _be_retry(f"/defi/txs/token/seek_by_time?address={mint}"
                      f"&before_time={before}&tx_type=swap&limit=50")
        time.sleep(RATE_SLEEP)
        items = ((t or {}).get("data") or {}).get("items") or []
        if not items:
            reached = True  # no more trades = whole available history covered
            break
        for it in items:
            ts = int(it.get("blockUnixTime") or 0)
            oldest = min(oldest, ts) if ts else oldest
            if ts < window_start or ts >= run_start_ts:
                continue
            base = it.get("base") or {}
            if float(base.get("uiChangeAmount") or 0) <= 0:  # buy = received the token
                continue
            owner = it.get("owner")
            if not owner:
                continue
            usd = _leg_usd(it)
            if usd < DUST_USD:
                continue
            a = agg.setdefault(owner, {"n_buys": 0, "bought_usd": 0.0, "first_buy_ts": ts})
            a["n_buys"] += 1
            a["bought_usd"] += usd
            a["first_buy_ts"] = min(a["first_buy_ts"], ts)
        if oldest <= window_start:
            reached = True
            break
        before = oldest - 1
    if not reached:
        log.info("prerun_window_truncated", mint=mint[:10],
                 oldest_day=datetime.fromtimestamp(oldest, tz=timezone.utc).date().isoformat(),
                 target_day=datetime.fromtimestamp(window_start, tz=timezone.utc).date().isoformat())
    # accumulator filter: real committed money, not a single dust nibble
    accums = {w: a for w, a in agg.items() if a["bought_usd"] >= MIN_PRERUN_USD}
    return accums, (not reached)


# --------------------------- 4/5. persist + report --------------------------
DDL = """
CREATE TABLE IF NOT EXISTS prerun_accumulators (
    id           BIGSERIAL PRIMARY KEY,
    dedup_key    VARCHAR(256) NOT NULL UNIQUE,
    token        VARCHAR(128) NOT NULL,
    symbol       VARCHAR(64),
    run_date     DATE,
    run_x        DOUBLE PRECISION,
    wallet       VARCHAR(64) NOT NULL,
    first_buy    TIMESTAMPTZ,
    lead_days    DOUBLE PRECISION,
    n_buys       INTEGER,
    bought_usd   DOUBLE PRECISION,
    pool_tier    VARCHAR(32),
    source       VARCHAR(64),
    observed     DATE,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_prerun_wallet ON prerun_accumulators (wallet);
CREATE INDEX IF NOT EXISTS ix_prerun_token ON prerun_accumulators (token);
CREATE TABLE IF NOT EXISTS prerun_scans (
    id           BIGSERIAL PRIMARY KEY,
    token        VARCHAR(128) NOT NULL,
    run_date     DATE NOT NULL,
    symbol       VARCHAR(64),
    run_x        DOUBLE PRECISION,
    accums       INTEGER,
    truncated    BOOLEAN,
    birdeye_calls INTEGER,
    observed     DATE,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (token, run_date)
);
CREATE TABLE IF NOT EXISTS wallet_activity (
    wallet         VARCHAR(64) PRIMARY KEY,
    trades_per_day DOUBLE PRECISION,
    sample_trades  INTEGER,
    span_hours     DOUBLE PRECISION,
    measured_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Persistent watchlist of wallets recurring across >= MIN_RECUR runners. Upserted every
-- run so a promising accumulator's emergence + history is tracked over time (first_seen,
-- peak_runners) instead of only re-derived into the nightly log. peak_runners is kept even
-- if a wallet later drops below threshold. A manual status (validated/rejected/promoted/
-- watching) is preserved across runs so vetting decisions aren't overwritten by the auto pass.
CREATE TABLE IF NOT EXISTS recurrence_candidates (
    wallet         VARCHAR(64) PRIMARY KEY,
    runners        INTEGER,
    peak_runners   INTEGER,
    avg_lead_days  DOUBLE PRECISION,
    avg_usd        DOUBLE PRECISION,
    trades_per_day DOUBLE PRECISION,
    is_bot         BOOLEAN,
    tier           VARCHAR(32),
    status         VARCHAR(32) NOT NULL DEFAULT 'candidate',
    first_seen     DATE NOT NULL DEFAULT CURRENT_DATE,
    last_seen      DATE NOT NULL DEFAULT CURRENT_DATE,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_reccand_status ON recurrence_candidates (status);
CREATE INDEX IF NOT EXISTS ix_reccand_genuine ON recurrence_candidates (is_bot, peak_runners);
"""

UPSERT = text("""
INSERT INTO prerun_accumulators
  (dedup_key, token, symbol, run_date, run_x, wallet, first_buy, lead_days,
   n_buys, bought_usd, pool_tier, source, observed)
VALUES
  (:dedup_key, :token, :symbol, :run_date, :run_x, :wallet, :first_buy, :lead_days,
   :n_buys, :bought_usd, :pool_tier, :source, :observed)
ON CONFLICT (dedup_key) DO UPDATE SET
  n_buys = EXCLUDED.n_buys, bought_usd = EXCLUDED.bought_usd,
  lead_days = EXCLUDED.lead_days, pool_tier = EXCLUDED.pool_tier
""")

# Upsert into the persistent candidate watchlist. peak_runners never shrinks; a manually
# set status (validated/rejected/promoted/watching) is preserved so vetting isn't clobbered.
REC_CAND_UPSERT = text("""
INSERT INTO recurrence_candidates
  (wallet, runners, peak_runners, avg_lead_days, avg_usd, trades_per_day,
   is_bot, tier, status, first_seen, last_seen, updated_at)
VALUES
  (:wallet, :runners, :runners, :avg_lead_days, :avg_usd, :tpd,
   :is_bot, :tier, :status, CURRENT_DATE, CURRENT_DATE, now())
ON CONFLICT (wallet) DO UPDATE SET
  runners        = EXCLUDED.runners,
  peak_runners   = GREATEST(recurrence_candidates.peak_runners, EXCLUDED.runners),
  avg_lead_days  = EXCLUDED.avg_lead_days,
  avg_usd        = EXCLUDED.avg_usd,
  trades_per_day = EXCLUDED.trades_per_day,
  is_bot         = EXCLUDED.is_bot,
  tier           = EXCLUDED.tier,
  last_seen      = CURRENT_DATE,
  updated_at     = now(),
  status = CASE
             WHEN recurrence_candidates.status IN
                  ('validated','rejected','promoted','watching')
               THEN recurrence_candidates.status
             ELSE EXCLUDED.status
           END
""")


def _pool_tiers(wallets: list[str]) -> dict[str, str]:
    if not wallets:
        return {}
    with session_scope() as s:
        rows = s.execute(
            text("SELECT address, tier FROM wallet_pool WHERE address = ANY(:w)"),
            {"w": wallets},
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def _measure_wallet_activity(wallet: str) -> tuple[float, int, float]:
    """Trades/day from a wallet's most-recent ~100 swaps (Birdeye trader trade feed).
    High frequency = automated bot/router/MEV, which disqualifies a 'pre-run
    accumulator' as a followable deliberate accumulator."""
    now = int(time.time())
    b = _be_retry(f"/trader/txs/seek_by_time?address={wallet}"
                  f"&before_time={now}&tx_type=swap&limit=100")
    time.sleep(RATE_SLEEP)
    items = ((b or {}).get("data") or {}).get("items") or []
    ts = [int(it.get("block_unix_time") or 0) for it in items if it.get("block_unix_time")]
    if len(ts) < 2:
        return 0.0, len(ts), 0.0
    span_h = (max(ts) - min(ts)) / 3600.0
    tpd = len(ts) / (span_h / 24.0) if span_h > 0 else 1e9  # 100 trades in ~0 span = extreme bot
    return round(tpd, 1), len(ts), round(span_h, 2)


def _wallet_trades_per_day(wallet: str) -> float:
    """Cached trades/day — re-measures only if missing or older than ACTIVITY_TTL_DAYS."""
    with session_scope() as s:
        row = s.execute(
            text("SELECT trades_per_day, measured_at FROM wallet_activity WHERE wallet = :w"),
            {"w": wallet},
        ).fetchone()
    if row and row[1] and (datetime.now(timezone.utc) - row[1]).days < ACTIVITY_TTL_DAYS:
        return float(row[0] or 0.0)
    tpd, n, span = _measure_wallet_activity(wallet)
    with session_scope() as s:
        s.execute(text("""
            INSERT INTO wallet_activity (wallet, trades_per_day, sample_trades, span_hours, measured_at)
            VALUES (:w, :t, :n, :s, now())
            ON CONFLICT (wallet) DO UPDATE SET
              trades_per_day = :t, sample_trades = :n, span_hours = :s, measured_at = now()
        """), {"w": wallet, "t": tpd, "n": n, "s": span})
    return tpd


def _already_scanned(mint: str, run_start_ts: int) -> bool:
    """True if we already scanned THIS run — a prerun_scans ledger row for this mint
    with a run_date within RESCAN_TOLERANCE_DAYS of the detected run_start. A new run
    (weeks later) falls outside the tolerance and is scanned fresh. Guards the
    EXPENSIVE seek_by_time paging; the cheap run-start history call still runs so a
    genuine re-run is detected. The LEDGER (not the observations table) is the source
    of truth so busy tokens that yielded 0 accumulators are also not re-paged."""
    d = datetime.fromtimestamp(run_start_ts, tz=timezone.utc).date()
    lo, hi = d - timedelta(days=RESCAN_TOLERANCE_DAYS), d + timedelta(days=RESCAN_TOLERANCE_DAYS)
    with session_scope() as s:
        n = s.execute(
            text("SELECT COUNT(*) FROM prerun_scans "
                 "WHERE token = :t AND run_date BETWEEN :lo AND :hi"),
            {"t": mint, "lo": lo, "hi": hi},
        ).scalar()
    return (n or 0) > 0


def record_scan(mint: str, symbol: str, run_start_ts: int, run_x: float,
                accums: int, truncated: bool, calls: int) -> None:
    """Ledger a completed scan (even 0-accumulator ones) so we never re-page it and
    so per-token Birdeye cost is measurable."""
    run_date = datetime.fromtimestamp(run_start_ts, tz=timezone.utc).date()
    with session_scope() as s:
        s.execute(text("""
            INSERT INTO prerun_scans
              (token, run_date, symbol, run_x, accums, truncated, birdeye_calls, observed)
            VALUES (:t, :rd, :sym, :rx, :ac, :tr, :ca, :ob)
            ON CONFLICT (token, run_date) DO UPDATE SET
              accums = EXCLUDED.accums, truncated = EXCLUDED.truncated,
              birdeye_calls = EXCLUDED.birdeye_calls, observed = EXCLUDED.observed
        """), {"t": mint, "rd": run_date, "sym": symbol[:64] if symbol else None,
               "rx": round(run_x, 2), "ac": accums, "tr": truncated,
               "ca": calls, "ob": datetime.now(timezone.utc).date()})


def persist(mint: str, symbol: str, run_start_ts: int, run_x: float,
            accums: dict[str, dict], tiers: dict[str, str]) -> int:
    run_date = datetime.fromtimestamp(run_start_ts, tz=timezone.utc).date()
    today = datetime.now(timezone.utc).date()
    rows = []
    for w, a in accums.items():
        rows.append({
            "dedup_key": f"{mint}:{w}:{run_date.isoformat()}",
            "token": mint, "symbol": symbol[:64] if symbol else None,
            "run_date": run_date, "run_x": round(run_x, 2), "wallet": w,
            "first_buy": datetime.fromtimestamp(a["first_buy_ts"], tz=timezone.utc),
            "lead_days": round((run_start_ts - a["first_buy_ts"]) / 86400, 3),
            "n_buys": a["n_buys"], "bought_usd": round(a["bought_usd"], 2),
            "pool_tier": tiers.get(w, "none"), "source": "prerun_cron", "observed": today,
        })
    if not rows:
        return 0
    with session_scope() as s:
        s.execute(UPSERT, rows)
    return len(rows)


def recurrence_report() -> None:
    """Wallets recurring across >= MIN_RECUR runners in the window, NOT already
    on an active roster — staged candidates for vetting / forward-test."""
    with session_scope() as s:
        rows = s.execute(text(f"""
            SELECT wallet,
                   COUNT(DISTINCT token) AS runners,
                   ROUND(AVG(lead_days)::numeric, 2) AS avg_lead_days,
                   ROUND(AVG(bought_usd)::numeric, 0) AS avg_usd,
                   MAX(pool_tier) AS tier
            FROM prerun_accumulators
            WHERE observed >= (CURRENT_DATE - INTERVAL '{RECUR_WINDOW_DAYS} days')
            GROUP BY wallet
            HAVING COUNT(DISTINCT token) >= {MIN_RECUR}
            ORDER BY runners DESC, avg_usd DESC
            LIMIT 200
        """)).fetchall()
    # Classify each candidate by trades/day (cached). Recurrence alone surfaces bots;
    # the frequency gate isolates genuine low-frequency deliberate accumulators.
    graded = [(w, n, lead, usd, tier, _wallet_trades_per_day(w))
              for (w, n, lead, usd, tier) in rows]
    accum = sorted((g for g in graded if g[5] <= BOT_FREQ_PER_DAY),
                   key=lambda g: (-g[1], -float(g[3] or 0)))
    bots = sorted((g for g in graded if g[5] > BOT_FREQ_PER_DAY), key=lambda g: -g[1])
    # Persist the watchlist ledger so promising wallets are tracked over time (not just
    # printed). Manual vetting statuses are preserved by REC_CAND_UPSERT.
    with session_scope() as s:
        for w, n, lead, usd, tier, tpd in graded:
            s.execute(REC_CAND_UPSERT, {
                "wallet": w, "runners": n, "avg_lead_days": lead, "avg_usd": usd,
                "tpd": tpd, "is_bot": tpd > BOT_FREQ_PER_DAY, "tier": tier,
                "status": "bot" if tpd > BOT_FREQ_PER_DAY else "candidate",
            })
    log.info("recurrence_report", n=len(rows), accumulators=len(accum),
             bots_filtered=len(bots), ledger_upserted=len(graded))
    print(f"\n=== Recurring pre-run accumulators (>= {MIN_RECUR} runners, last {RECUR_WINDOW_DAYS}d) ===")
    print(f"{len(bots)} filtered as BOTS (> {BOT_FREQ_PER_DAY:.0f} trades/day); "
          f"{len(accum)} genuine low-frequency accumulators — CANDIDATES (not auto-promoted):")
    print(f"{'wallet':<46} {'runners':>7} {'trades/d':>9} {'avg_lead_d':>10} {'avg_usd':>10} {'tier':>10}")
    for w, n, lead, usd, tier, tpd in accum:
        flag = "" if tier in (None, "none") else "  <-- already in pool"
        print(f"{w:<46} {n:>7} {tpd:>9.0f} {lead:>10} {usd:>10} {(tier or 'none'):>10}{flag}")
    if not accum:
        print("  (none yet — every recurring wallet so far is a high-frequency bot; "
              "genuine accumulators need corpus growth + broader discovery)")
    if bots:
        print("  [top filtered bots]: " + ", ".join(
            f"{w[:8]}(rn={n},{tpd:.0f}/d)" for w, n, lead, usd, tier, tpd in bots[:6]))


def main() -> None:
    global _BE_CALLS
    _BE_CALLS = 0
    if not _KEY:
        log.error("scrape_no_birdeye_key")
        return
    with session_scope() as s:
        for stmt in DDL.strip().split(";"):
            if stmt.strip():
                s.execute(text(stmt))
    runners = discover_runners()
    log.info("scrape_discovered", n=len(runners))
    print(f"Discovered {len(runners)} candidate runners (liq>={MIN_LIQ_USD:.0f}, "
          f"vol24>={MIN_VOL24_USD:.0f}); processing up to {MAX_TOKENS}.")
    processed = confirmed = skipped = total_rows = 0
    for r in runners[:MAX_TOKENS]:
        mint, sym = r["mint"], r["symbol"]
        try:
            run = detect_run_start(mint)  # 1 cheap Birdeye call; detects re-runs
        except Exception as e:  # noqa: BLE001
            log.warning("run_detect_failed", mint=mint[:10], err=str(e))
            continue
        processed += 1
        if not run:
            continue
        confirmed += 1
        rs = datetime.fromtimestamp(run["run_start_ts"], tz=timezone.utc).date()
        # Skip the EXPENSIVE pre-run paging if we already captured this run.
        if _already_scanned(mint, run["run_start_ts"]):
            skipped += 1
            print(f"  {sym:<16} {mint[:10]} run_start={rs} — already scanned, skip")
            continue
        calls_before = _BE_CALLS
        try:
            accums, truncated = prerun_accumulators(mint, run["run_start_ts"])
        except Exception as e:  # noqa: BLE001
            log.warning("prerun_fetch_failed", mint=mint[:10], err=str(e))
            continue
        tiers = _pool_tiers(list(accums.keys()))
        n = persist(mint, sym, run["run_start_ts"], run["run_x"], accums, tiers)
        record_scan(mint, sym, run["run_start_ts"], run["run_x"],
                    len(accums), truncated, _BE_CALLS - calls_before)
        total_rows += n
        known = sum(1 for w in accums if tiers.get(w, "none") != "none")
        print(f"  {sym:<16} {mint[:10]} run_start={rs} run_x={run['run_x']:.1f} "
              f"accumulators={len(accums)} known_in_pool={known} persisted={n}")
    log.info("scrape_done", processed=processed, confirmed=confirmed,
             skipped=skipped, rows=total_rows, birdeye_calls=_BE_CALLS)
    print(f"\nProcessed {processed}, confirmed runs {confirmed}, "
          f"skipped {skipped} already-scanned, persisted {total_rows} obs. "
          f"Birdeye calls this run: {_BE_CALLS}.")
    recurrence_report()


if __name__ == "__main__":
    main()
