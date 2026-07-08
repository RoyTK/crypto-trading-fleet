"""Slow-cluster SHADOW detector — the forward test of Roy's thesis (2026-07-07):

  "3+ GENUINE (non-bot) wallets accumulate >= $200 of a token over its first few days
   AND hold into the run" -> does that predict a run?

SHADOW ONLY. Records a signal at a real point in time and tracks the forward outcome —
it NEVER trades. Fixes the two flaws of the historical reconstruction (no reference-timing
confound: we measure genuinely forward from the scan; captures REAL duds: every scanned
token is recorded, signal AND control).

Daily flow:
  1. discover recent traction tokens aged AGE_LO..AGE_HI days (past the accumulation
     window, early enough to measure forward), liq >= MIN_LIQ, not already scanned.
  2. per token: early accumulators (>= $200 buys in [launch, launch+ACCUM_DAYS], TOKEN
     feed camelCase, back-paged) -> bot-filter (trades/day <= BOT_TPD) + held-check (no
     pre-reference sell) -> n_genuine_holders. Record token + n_holders + price/liq now.
  3. is_signal = n_genuine_holders >= MIN_HOLDERS.
  4. update_forward: for every recorded row (< RESOLVE_DAYS old), refresh fwd_mult_max =
     max price since scan / price at scan.

Decide after ~3-4 weeks: compare fwd_mult for is_signal vs control, incl duds.

Runs IN-CONTAINER (framework/bot_copy: birdeye_api_key + DB + repo mounted). Env-overridable.
"""
from __future__ import annotations
import json, os, time, urllib.request, urllib.error
from collections import defaultdict

from sqlalchemy import text
from framework.db import session_scope
from framework.logging_setup import get_logger

from bots.copy.config import get_copy_settings

log = get_logger("slow_cluster_detector")
_KEY = get_copy_settings().birdeye_api_key
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"

ACCUM_DAYS   = int(os.getenv("SC_ACCUM_DAYS", "3"))
MIN_ACCUM_USD = float(os.getenv("SC_MIN_ACCUM_USD", "200"))
BOT_TPD      = float(os.getenv("SC_BOT_TPD", "50"))
MIN_HOLDERS  = int(os.getenv("SC_MIN_HOLDERS", "3"))
AGE_LO       = float(os.getenv("SC_AGE_LO", "3"))
AGE_HI       = float(os.getenv("SC_AGE_HI", "6"))
MIN_LIQ      = float(os.getenv("SC_MIN_LIQ", "30000"))
MAX_EVAL     = int(os.getenv("SC_MAX_EVAL", "40"))     # new tokens evaluated per run
MAX_SCAN     = int(os.getenv("SC_MAX_SCAN", "400"))    # cap age-check (security) calls/run
MAX_CAND     = int(os.getenv("SC_MAX_CAND", "30"))     # accumulators checked per token
RESOLVE_DAYS = int(os.getenv("SC_RESOLVE_DAYS", "30")) # stop refreshing after this
RATE = 0.18

QUOTES = {"So11111111111111111111111111111111111111112",
          "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
          "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}

DDL = """
CREATE TABLE IF NOT EXISTS slow_cluster_signals (
    token          VARCHAR(128) PRIMARY KEY,
    symbol         VARCHAR(64),
    scanned_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    scan_unix      BIGINT NOT NULL,
    age_days       DOUBLE PRECISION,
    n_accum_200    INTEGER,
    n_genuine      INTEGER,
    n_holders      INTEGER,
    is_signal      BOOLEAN,
    price_at_scan  DOUBLE PRECISION,
    liq_at_scan    DOUBLE PRECISION,
    fwd_mult_max   DOUBLE PRECISION,
    resolved       BOOLEAN NOT NULL DEFAULT false,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_sc_signal ON slow_cluster_signals (is_signal, scanned_at);
"""


def _be(path):
    r = urllib.request.Request("https://public-api.birdeye.so" + path,
        headers={"X-API-KEY": _KEY, "x-chain": "solana", "Accept": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(r, timeout=25) as resp:
        return json.loads(resp.read().decode())


def _be_retry(path, tries=3):
    for i in range(tries):
        try:
            return _be(path)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(4); continue
            return None
        except Exception:
            time.sleep(1.3)
    return None


def _leg_usd(it):
    q = it.get("quote") or {}
    if q.get("address") in QUOTES and q.get("uiAmount") and q.get("price"):
        return abs(float(q["uiAmount"])) * float(q["price"])
    b = it.get("base") or {}
    tp = it.get("tokenPrice")
    if b.get("uiAmount") and tp:
        return abs(float(b["uiAmount"])) * float(tp)
    return 0.0


def _already_scanned() -> set[str]:
    with session_scope() as s:
        return {r[0] for r in s.execute(text("SELECT token FROM slow_cluster_signals")).fetchall()}


def discover(now: int, skip: set[str]) -> list[dict]:
    """v3 traction tokens aged AGE_LO..AGE_HI days, liq>=MIN_LIQ, not yet scanned."""
    out = []; scanned = 0
    for off in range(0, 1400, 20):
        d = _be_retry(f"/defi/v3/token/list?sort_by=volume_24h_usd&sort_type=desc"
                      f"&offset={off}&limit=20&min_liquidity={int(MIN_LIQ)}")
        time.sleep(RATE)
        items = ((d or {}).get("data") or {}).get("items") or \
                ((d or {}).get("data") or {}).get("tokens") or []
        if not items:
            break
        for it in items:
            a = it.get("address")
            if not a or a in skip:
                continue
            if scanned >= MAX_SCAN:
                return out
            scanned += 1
            sec = _be_retry(f"/defi/token_security?address={a}"); time.sleep(RATE)
            ct = ((sec or {}).get("data") or {}).get("creationTime")
            if not ct:
                continue
            age = (now - int(ct)) / 86400.0
            if AGE_LO <= age <= AGE_HI:
                out.append({"mint": a, "sym": it.get("symbol"), "launch": int(ct), "age": age,
                            "liq": float(it.get("liquidity") or 0)})
                if len(out) >= MAX_EVAL:
                    return out
    return out


def early_accumulators(token: str, launch: int, reference: int) -> dict[str, float]:
    buys: dict[str, float] = defaultdict(float)
    before = reference; oldest = reference
    for _ in range(10):
        d = _be_retry(f"/defi/txs/token/seek_by_time?address={token}"
                      f"&before_time={before}&tx_type=swap&limit=50")
        time.sleep(RATE)
        items = ((d or {}).get("data") or {}).get("items") or []
        if not items:
            break
        for it in items:
            ts = int(it.get("blockUnixTime") or 0)
            if ts:
                oldest = min(oldest, ts)
            if ts < launch or ts >= reference:
                continue
            base = it.get("base") or {}
            if float(base.get("uiChangeAmount") or 0) <= 0:
                continue
            owner = it.get("owner")
            if owner:
                buys[owner] += _leg_usd(it)
        if oldest <= launch:
            break
        before = oldest - 1
    return {w: u for w, u in buys.items() if u >= MIN_ACCUM_USD}


def tpd_and_held(wallet: str, token: str, reference: int, first_buy: int) -> tuple[float, bool]:
    """One trader-feed call: (trades/day, sold_token_before_reference). Trader feed = snake_case."""
    b = _be_retry(f"/trader/txs/seek_by_time?address={wallet}"
                  f"&before_time={reference + 3600}&tx_type=swap&limit=100")
    time.sleep(RATE)
    items = ((b or {}).get("data") or {}).get("items") or []
    if not items:
        return 9999.0, False
    ts = [int(it.get("block_unix_time") or 0) for it in items if it.get("block_unix_time")]
    span = (max(ts) - min(ts)) / 86400.0 if len(ts) >= 2 else 0
    tpd = len(ts) / span if span > 0 else 9999.0
    sold = any(
        (it.get("base") or {}).get("address") == token
        and float((it.get("base") or {}).get("ui_change_amount") or 0) < 0
        and first_buy <= int(it.get("block_unix_time") or 0) <= reference
        for it in items
    )
    return round(tpd, 1), sold


def evaluate(tok: dict, now: int) -> dict | None:
    launch = tok["launch"]; reference = launch + ACCUM_DAYS * 86400
    if reference > now:
        return None
    accum = early_accumulators(tok["mint"], launch, reference)
    genuine = holders = 0
    for w, _usd in sorted(accum.items(), key=lambda kv: -kv[1])[:MAX_CAND]:
        tpd, sold = tpd_and_held(w, tok["mint"], reference, launch)
        if tpd <= BOT_TPD:
            genuine += 1
            if not sold:
                holders += 1
    pr = _be_retry(f"/defi/price?address={tok['mint']}"); time.sleep(RATE)
    price = ((pr or {}).get("data") or {}).get("value")
    return {"mint": tok["mint"], "sym": tok["sym"], "age": tok["age"], "liq": tok["liq"],
            "n_accum": len(accum), "genuine": genuine, "holders": holders, "price": price}


def record(ev: dict, now: int) -> None:
    with session_scope() as s:
        s.execute(text("""
            INSERT INTO slow_cluster_signals
              (token, symbol, scan_unix, age_days, n_accum_200, n_genuine, n_holders,
               is_signal, price_at_scan, liq_at_scan, fwd_mult_max)
            VALUES (:t,:sym,:su,:age,:na,:g,:h,:sig,:px,:liq,1.0)
            ON CONFLICT (token) DO NOTHING
        """), {"t": ev["mint"], "sym": ev["sym"], "su": now, "age": round(ev["age"], 2),
               "na": ev["n_accum"], "g": ev["genuine"], "h": ev["holders"],
               "sig": ev["holders"] >= MIN_HOLDERS, "px": ev["price"], "liq": ev["liq"]})
        s.execute(text("COMMIT"))


def update_forward(now: int) -> int:
    """Refresh fwd_mult_max for unresolved rows via history_price since scan."""
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT token, scan_unix, price_at_scan FROM slow_cluster_signals
            WHERE NOT resolved AND price_at_scan IS NOT NULL AND price_at_scan > 0
        """)).fetchall()
    n = 0
    for token, scan_unix, px0 in rows:
        hp = _be_retry(f"/defi/history_price?address={token}&address_type=token&type=1H"
                       f"&time_from={int(scan_unix)-1800}&time_to={now}")
        time.sleep(RATE)
        pts = [float(p["value"]) for p in (((hp or {}).get("data") or {}).get("items") or [])
               if p.get("value")]
        if not pts:
            continue
        fwd = max(pts) / float(px0)
        resolved = (now - int(scan_unix)) > RESOLVE_DAYS * 86400
        with session_scope() as s:
            s.execute(text("""UPDATE slow_cluster_signals
                SET fwd_mult_max=:f, resolved=:r, updated_at=now() WHERE token=:t"""),
                {"f": round(fwd, 3), "r": resolved, "t": token})
            s.execute(text("COMMIT"))
        n += 1
    return n


def main() -> None:
    if not _KEY:
        log.error("slow_cluster_no_key"); return
    now = int(time.time())
    with session_scope() as s:
        for stmt in DDL.strip().split(";"):
            if stmt.strip():
                s.execute(text(stmt))
        s.execute(text("COMMIT"))
    skip = _already_scanned()
    cands = discover(now, skip)
    log.info("slow_cluster_discover", candidates=len(cands), already=len(skip))
    recorded = signals = 0
    for tok in cands:
        ev = evaluate(tok, now)
        if ev is None:
            continue
        record(ev, now)
        recorded += 1
        if ev["holders"] >= MIN_HOLDERS:
            signals += 1
        print(f"  {str(ev['sym'])[:12]:>12} age{ev['age']:.1f}d accum={ev['n_accum']:3d} "
              f"genuine={ev['genuine']:2d} holders={ev['holders']:2d} "
              f"{'** SIGNAL' if ev['holders']>=MIN_HOLDERS else ''}", flush=True)
    updated = update_forward(now)
    log.info("slow_cluster_done", recorded=recorded, signals=signals, forward_updated=updated)
    # quick standing tally
    with session_scope() as s:
        for lbl, cond in (("SIGNAL(3+)", "is_signal"), ("control(<3)", "NOT is_signal")):
            r = s.execute(text(f"""SELECT count(*), round(avg(fwd_mult_max)::numeric,2),
                round((count(*) FILTER (WHERE fwd_mult_max>=2))::numeric*100/NULLIF(count(*),0),0)
                FROM slow_cluster_signals WHERE {cond}""")).fetchone()
            print(f"  {lbl:>12}: n={r[0]}  avgFwd={r[1]}  ran>=2x={r[2]}%")


if __name__ == "__main__":
    main()
