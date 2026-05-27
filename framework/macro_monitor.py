"""Macro shock monitor — kill-switch + geo-shock alert.

Two functions called by the scoring engine cron:

1. `check_macro_kill_switch()` — 5-min cadence. If VIX > 30 AND BTC intraday
   move < -5%, emit P1 alert. If MACRO_KILL_SWITCH_AUTO_HALT_ENABLED env
   is true, also halt both bots via halt_bot(). Default alert-only per
   Roy's "I rely on my judgment" preference. Optimist's R3 conviction
   proposal — convexity/insurance primitive, not a pattern claim.

2. `check_geo_shock_alert()` — 60-min cadence. If (VIX > 20 sustained 2+
   days) AND (DXY weakening) AND (10Y yield not rising) → emit P2 alert.
   Geo-political-expert's R2 refinement — DXY is the true conditional,
   not VIX alone. Alert-only (research-grade, no halt).

Data sources (all free, no auth):
- VIX: Yahoo Finance ^VIX (daily close)
- DXY: Yahoo Finance DX-Y.NYB (daily close)
- 10Y yield: Yahoo Finance ^TNX (daily close)
- BTC intraday: CoinGecko /coins/bitcoin/market_chart?days=1

Built 2026-05-27 per the adversarial team meeting verdict (see
memory/project_decision_log.md, entry 2026-05-26).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from framework.alerts import emit_alert
from framework.audit import write_audit
from framework.halt_state import halt_bot, is_bot_halted
from framework.logging_setup import get_logger
from monitoring.alerting.taxonomy import Severity


log = get_logger(__name__)


# Thresholds (signed 2026-05-26 per adversarial team meeting on the
# correlation research thesis)
KILL_SWITCH_VIX_THRESHOLD = 30.0      # VIX above this is "panic regime"
KILL_SWITCH_BTC_DROP_PCT = -5.0       # BTC intraday move below this
KILL_SWITCH_AUTO_HALT_ENV = "MACRO_KILL_SWITCH_AUTO_HALT_ENABLED"

GEO_SHOCK_VIX_THRESHOLD = 20.0
GEO_SHOCK_VIX_SUSTAINED_DAYS = 2

ALERT_THROTTLE_SECONDS = 3600  # 1 alert per condition per hour


# In-process throttle (same pattern as dd_monitor + kill_criteria fallback)
_last_alert_ts: dict[str, float] = {}


def _throttled(key: str, seconds: int = ALERT_THROTTLE_SECONDS) -> bool:
    """True if we already alerted on this key within `seconds`."""
    now = time.time()
    last = _last_alert_ts.get(key, 0.0)
    if now - last < seconds:
        return True
    _last_alert_ts[key] = now
    return False


# ---------------------------------------------------------------------------
# Data fetchers (free public APIs)
# ---------------------------------------------------------------------------

def _yahoo_chart_latest(symbol: str) -> dict | None:
    """Yahoo Finance chart API — returns dict with 'close' (latest close)
    and 'prev_closes' (last 5 closes including latest)."""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?range=5d&interval=1d"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        result = data.get("chart", {}).get("result", [{}])[0]
        closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        valid = [c for c in closes if c is not None]
        if not valid:
            return None
        return {"close": float(valid[-1]), "prev_closes": [float(c) for c in valid]}
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError, TimeoutError):
        log.warning("yahoo_chart_fetch_failed", symbol=symbol)
        return None


def _coingecko_btc_intraday() -> float | None:
    """BTC intraday return: latest price vs price ~24h ago. Returns pct change."""
    url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=1&interval=hourly"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        prices = data.get("prices", [])
        if len(prices) < 2:
            return None
        first = prices[0][1]
        last = prices[-1][1]
        if first <= 0:
            return None
        return (last - first) / first * 100.0
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError, TimeoutError):
        log.warning("coingecko_btc_intraday_fetch_failed")
        return None


# ---------------------------------------------------------------------------
# Monitor 1 — Kill-switch (5-min cadence)
# ---------------------------------------------------------------------------

def check_macro_kill_switch() -> None:
    """High-urgency check. Both conditions must fire for any action.

    Convexity claim per Optimist: this is insurance, not a pattern bet.
    The cost of a false positive is missing some upside; the cost of a
    false negative is fleet-wide drawdown during a real shock. Asymmetric.
    """
    vix_data = _yahoo_chart_latest("%5EVIX")  # URL-encoded ^VIX
    btc_intraday = _coingecko_btc_intraday()

    if vix_data is None or btc_intraday is None:
        # Data fetch failed — don't act, don't spam
        return

    vix = vix_data["close"]
    triggered = vix > KILL_SWITCH_VIX_THRESHOLD and btc_intraday < KILL_SWITCH_BTC_DROP_PCT

    if not triggered:
        return

    auto_halt = os.environ.get(KILL_SWITCH_AUTO_HALT_ENV, "false").lower() in ("true", "1", "yes")

    body_lines = [
        f"Macro shock kill-switch conditions met:",
        f"  VIX = {vix:.2f}  (threshold > {KILL_SWITCH_VIX_THRESHOLD})",
        f"  BTC 24h move = {btc_intraday:+.2f}%  (threshold < {KILL_SWITCH_BTC_DROP_PCT}%)",
        "",
    ]

    if auto_halt:
        # Halt both bots — pattern matches dd_monitor.
        halted = []
        for bot_id in ("structure", "copy"):
            if is_bot_halted(bot_id):
                continue
            halt_bot(
                bot_id,
                halt_type="macro_shock_kill_switch",
                reason=f"VIX={vix:.2f} + BTC24h={btc_intraday:+.2f}%",
                severity="p1",
                metadata={"vix": vix, "btc_24h_pct": btc_intraday},
            )
            halted.append(bot_id)
        body_lines.append(f"AUTO-HALT FIRED: {', '.join(halted) if halted else '(none — all bots already halted)'}")
        body_lines.append("Manual review required to resume. Per the kill-criteria signed 2026-05-25, Roy retains judgment on resume.")
    else:
        body_lines.append("AUTO-HALT IS DISABLED (default). To enable auto-halt on future shocks:")
        body_lines.append(f"  Set {KILL_SWITCH_AUTO_HALT_ENV}=true in .env")
        body_lines.append(f"  Then: docker compose up -d --force-recreate scoring")
        body_lines.append("Manual decision: review market, decide whether to halt bots manually via SQL or /panic.")

    # Throttle alerts so we don't spam every 5 min during sustained event
    if _throttled("macro_kill_switch"):
        return

    emit_alert(
        severity=Severity.P1,
        title=f"[fleet] MACRO SHOCK kill-switch fired: VIX={vix:.1f}, BTC 24h={btc_intraday:+.1f}%",
        body="\n".join(body_lines),
        event_type="macro_shock_kill_switch",
        metadata={
            "vix": vix,
            "btc_24h_pct": btc_intraday,
            "auto_halt_enabled": auto_halt,
        },
    )
    write_audit(
        "macro_shock_kill_switch_fired",
        payload={"vix": vix, "btc_24h_pct": btc_intraday, "auto_halt": auto_halt},
    )


# ---------------------------------------------------------------------------
# Monitor 2 — Geo-shock alert (60-min cadence, research-grade)
# ---------------------------------------------------------------------------

def check_geo_shock_alert() -> None:
    """Geo-political shock condition per Geo's R2 refinement.

    The team's converged read: BTC outperforms during VIX>20 shocks UNLESS
    DXY is strengthening AND yields rising (then BTC fails to safe-haven).
    This is alert-only — Roy reviews manually, no automated trades.
    """
    vix = _yahoo_chart_latest("%5EVIX")
    dxy = _yahoo_chart_latest("DX-Y.NYB")
    tnx = _yahoo_chart_latest("%5ETNX")  # 10Y yield ticker

    if vix is None or dxy is None or tnx is None:
        return

    # VIX sustained > 20 for at least N days
    vix_closes = vix["prev_closes"][-GEO_SHOCK_VIX_SUSTAINED_DAYS:]
    vix_sustained_high = (
        len(vix_closes) >= GEO_SHOCK_VIX_SUSTAINED_DAYS
        and all(c > GEO_SHOCK_VIX_THRESHOLD for c in vix_closes)
    )

    # DXY weakening (last close < 5d-ago close) OR neutral
    dxy_weakening = dxy["close"] <= dxy["prev_closes"][0]
    # 10Y yields not rising (last close <= 5d-ago)
    yields_not_rising = tnx["close"] <= tnx["prev_closes"][0]

    if not (vix_sustained_high and dxy_weakening and yields_not_rising):
        return

    if _throttled("geo_shock_alert"):
        return

    emit_alert(
        severity=Severity.P2,
        title=f"[fleet] Geo-shock conditions met: VIX={vix['close']:.1f} (sustained)",
        body=(
            f"All three geo-shock-favorable conditions are currently true:\n"
            f"  VIX = {vix['close']:.2f}, sustained > {GEO_SHOCK_VIX_THRESHOLD} for {GEO_SHOCK_VIX_SUSTAINED_DAYS}+ days (last {GEO_SHOCK_VIX_SUSTAINED_DAYS}: {vix_closes})\n"
            f"  DXY = {dxy['close']:.2f}, 5d-ago = {dxy['prev_closes'][0]:.2f} ({'weakening or flat' if dxy_weakening else 'strengthening'})\n"
            f"  10Y yield = {tnx['close']:.2f}, 5d-ago = {tnx['prev_closes'][0]:.2f} ({'not rising' if yields_not_rising else 'rising'})\n\n"
            f"Per the adversarial team meeting (2026-05-26), this combo HISTORICALLY (small n)\n"
            f"preceded BTC short-window safe-haven outperformance (3-7 days). The trade is:\n"
            f"  Entry: BTC long at current price (manual review first)\n"
            f"  Exit: VIX < 18 OR day 7, whichever first\n\n"
            f"This is an ALERT, not an automated trade. Make your own call."
        ),
        event_type="geo_shock_alert",
        metadata={
            "vix": vix["close"],
            "dxy": dxy["close"],
            "tnx": tnx["close"],
            "vix_sustained_days": GEO_SHOCK_VIX_SUSTAINED_DAYS,
        },
    )


def run_all() -> None:
    """Called by scoring engine cron."""
    try:
        check_macro_kill_switch()
    except Exception:
        log.exception("macro_kill_switch_failed")
    try:
        check_geo_shock_alert()
    except Exception:
        log.exception("geo_shock_alert_failed")
