"""MELT-style signal feature logging — the labeled-dataset spine.

On every strategy signal we snapshot a feature-set (computed RIGHT at signal time,
which retro-backtesting on old tokens could not: live holder concentration, cohort
overlap, wash/order-flow, context) into `signal_features`, then backfill the forward
outcome from the linked trade. After enough rows this trains the multi-factor model
MELT (arXiv 2602.13480) proves works (84.6% precision; loss 61%→27%) — the honest
path the retro ceiling forced us to (see research/manipulation_backtest_findings_2026-07-12.md).

Reimplements MELT's FEATURE METHODS on our own data (methods, not the CC-BY-NC dataset).
Everything here is fail-open: a feature-fetch error must NEVER block a trade.

Not yet wired into the loop — increment 1 (safe foundation). Increment 2 calls
`record()` from each strategy's entry path + `backfill_outcomes()` from a cron.
"""
from __future__ import annotations

import json
import time
import urllib.request
import urllib.error

from sqlalchemy import text
from framework.db import session_scope
from framework.logging_setup import get_logger
from framework.api_usage import bump as _usage_bump
from bots.copy.config import get_copy_settings
from scripts.manipulation_detectors import analyze, buy_concentration, cohort_bundle_fraction

log = get_logger("signal_features")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126 Safari/537.36"

DDL = """
CREATE TABLE IF NOT EXISTS signal_features (
    id              BIGSERIAL PRIMARY KEY,
    strategy        VARCHAR(32) NOT NULL,
    token           VARCHAR(128) NOT NULL,
    signal_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    signal_unix     BIGINT NOT NULL,
    trigger_wallets JSON,
    entry_price     DOUBLE PRECISION,
    liquidity_usd   DOUBLE PRECISION,
    token_age_h     DOUBLE PRECISION,
    top_holder_pct  DOUBLE PRECISION,     -- MELT concentration (live top-k % of supply)
    n_holders       INTEGER,
    cohort_buyer_frac DOUBLE PRECISION,   -- MELT bundle (RED-COHORT overlap)
    n_cohort        INTEGER,
    zero_risk_frac  DOUBLE PRECISION,     -- wash (recent-window)
    features_json   JSON,                 -- extensible bag
    trade_id        INTEGER,              -- linked paper trade (if opened)
    outcome_pnl_pct DOUBLE PRECISION,     -- backfilled
    outcome_peak_pct DOUBLE PRECISION,
    resolved        BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS ix_sigfeat_strategy ON signal_features (strategy, signal_at);
CREATE INDEX IF NOT EXISTS ix_sigfeat_unresolved ON signal_features (resolved) WHERE NOT resolved;
"""

_COHORT: set[str] | None = None


def _cohort_set() -> set[str]:
    global _COHORT
    if _COHORT is None:
        for p in ("/app/research/redcohort_all_wallets.json", "research/redcohort_all_wallets.json"):
            try:
                _COHORT = set(json.load(open(p)))
                break
            except Exception:
                continue
        if _COHORT is None:
            _COHORT = set()
    return _COHORT


def _be(path: str):
    key = get_copy_settings().birdeye_api_key
    for i in range(2):
        try:
            r = urllib.request.Request(
                "https://public-api.birdeye.so" + path,
                headers={"X-API-KEY": key, "x-chain": "solana",
                         "Accept": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(r, timeout=15) as resp:
                _usage_bump("birdeye", 200)
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            _usage_bump("birdeye", e.code)
            if e.code == 429:
                time.sleep(3)
                continue
            return None
        except Exception:
            return None
    return None


def _live_top_holder_pct(token: str, top_k: int = 10):
    """MELT's dominant feature done RIGHT: top-k holders' share of supply, LIVE at
    signal time (retro couldn't do this). Fail-open -> (None, None)."""
    d = _be(f"/defi/v3/token/holder?address={token}&offset=0&limit={top_k}")
    items = ((d or {}).get("data") or {}).get("items") or []
    if not items:
        return None, None
    try:
        held = sum(float(h.get("ui_amount") or 0) for h in items)
        ov = _be(f"/defi/token_overview?address={token}") or {}
        supply = float(((ov.get("data") or {}).get("supply")) or 0)
        if supply > 0:
            return held / supply, len(items)
    except Exception:
        pass
    return None, len(items)


def record(strategy: str, token: str, *, entry_price=None, liquidity_usd=None,
           token_age_h=None, trigger_wallets=None, recent_trades=None,
           top_holder_pct: float | None = None, n_holders: int | None = None,
           extra: dict | None = None) -> int | None:
    """Insert a signal_features row. `recent_trades` (list of {owner,side,usd,...})
    powers cohort + wash features. Concentration (MELT's #1 feature): pass
    `top_holder_pct` from a source that works on fresh tokens (token_security's
    top10_holder_pct) — only when omitted do we fall back to the live holder-endpoint
    call, which returns null for pre-graduation bonding-curve tokens. Fully fail-open —
    returns row id or None, never raises into the caller."""
    try:
        now = int(time.time())
        top_pct = top_holder_pct
        if top_pct is None:
            top_pct, n_holders = _live_top_holder_pct(token)
        cohort_bf = n_coh = zr = None
        if recent_trades:
            _, cohort_bf, n_coh = cohort_bundle_fraction(recent_trades, _cohort_set())
            zr = analyze(recent_trades).zero_risk_vol_frac
        feats = dict(extra or {})
        with session_scope() as s:
            row = s.execute(text(
                "INSERT INTO signal_features (strategy, token, signal_unix, trigger_wallets, "
                "entry_price, liquidity_usd, token_age_h, top_holder_pct, n_holders, "
                "cohort_buyer_frac, n_cohort, zero_risk_frac, features_json) VALUES "
                "(:st,:tok,:u,:tw,:px,:liq,:age,:tp,:nh,:cbf,:nc,:zr,:fj) RETURNING id"
            ), {"st": strategy, "tok": token, "u": now,
                "tw": json.dumps(trigger_wallets or []), "px": entry_price,
                "liq": liquidity_usd, "age": token_age_h, "tp": top_pct, "nh": n_holders,
                "cbf": cohort_bf, "nc": n_coh, "zr": zr,
                "fj": json.dumps(feats)}).scalar()
        log.info("signal_features_recorded", strategy=strategy, token=token[:12],
                 top_holder_pct=top_pct, cohort_frac=cohort_bf)
        return int(row)
    except Exception as e:
        log.warning("signal_features_record_failed", strategy=strategy, err=str(e))
        return None


def first_buyer_features(fb: dict | None) -> dict:
    """Turn a fetch_first_buyers result into loggable bundle features: what fraction of
    the token's earliest buyers are known RED-COHORT coordinated wallets, plus the
    sniper dump rate. Empty dict if no data. Pure (uses the cached cohort set)."""
    if not fb or not fb.get("buyer_wallets"):
        return {}
    cset = _cohort_set()
    wallets = fb["buyer_wallets"]
    n = len(wallets)
    n_cohort = sum(1 for w in wallets if w in cset)
    return {
        "n_first_buyers": fb.get("n_first_buyers"),
        "first_buyer_cohort_frac": (n_cohort / n) if n else None,
        "n_first_buyer_cohort": n_cohort,
        "first_buyer_sell_all_frac": fb.get("first_buyer_sell_all_frac"),
        "first_buyer_hold_frac": fb.get("first_buyer_hold_frac"),
        "first_buyer_top5_vol_frac": fb.get("first_buyer_top5_vol_frac"),
        "first_buyer_bundler_frac": fb.get("first_buyer_bundler_frac"),
        "first_buyer_sniper_frac": fb.get("first_buyer_sniper_frac"),
    }


def link_trade(feature_id: int, trade_id: int) -> None:
    if not feature_id:
        return
    try:
        with session_scope() as s:
            s.execute(text("UPDATE signal_features SET trade_id=:t WHERE id=:i"),
                      {"t": trade_id, "i": feature_id})
    except Exception:
        pass


def backfill_outcomes() -> int:
    """Join resolved trades into their feature rows (run from a cron)."""
    try:
        with session_scope() as s:
            n = s.execute(text("""
                UPDATE signal_features sf
                SET outcome_pnl_pct = t.pnl_pct,
                    outcome_peak_pct = (t.sim_metadata->>'peak_pct_since_entry')::double precision,
                    resolved = true
                FROM trades t
                WHERE sf.trade_id = t.id AND NOT sf.resolved
                  AND t.fill_status = 'closed' AND t.exit_at IS NOT NULL
            """)).rowcount
        return n or 0
    except Exception as e:
        log.warning("signal_features_backfill_failed", err=str(e))
        return 0


def ensure_table() -> None:
    with session_scope() as s:
        for stmt in DDL.split(";"):
            if stmt.strip():
                s.execute(text(stmt))


if __name__ == "__main__":
    ensure_table()
    print("signal_features table ensured")
