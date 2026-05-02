"""Quarterly whale-list health check (Hetzner cron edition).

Replaces the remote-agent approach which fails because Anthropic cloud
egress IPs receive empty responses from the Hyperliquid Info API. This script
runs INSIDE the framework Docker container on Hetzner where the API works.

Behavior:
- Reads bots/structure/whale_list.json
- Fetches last-180-day fills for each whale via HL Info API
- Computes stats (win rate, closed positions, cumulative notional)
- Determines kept / would-be-dropped using locked thresholds
  (WR>=60%, trades>=20, notional>=$5M)
- Writes a `whale_refresh_*` audit_log row with the full summary in payload
- Emits a P2 Discord alert with the kept/dropped count + brief instructions
- Does NOT modify whale_list.json — Roy reviews and applies manually

Run via cron:
    crontab -e   # as fleet user
    0 14 1 2,5,8,11 * cd /home/fleet/crypto-fleet && /usr/bin/docker compose exec -T framework python -m scripts.quarterly_whale_refresh >> /home/fleet/logs/whale_refresh.log 2>&1

Run manually for testing:
    docker compose exec -T framework python -m scripts.quarterly_whale_refresh
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from framework.alerts import emit_alert
from framework.audit import write_audit
from framework.logging_setup import configure_logging, get_logger
from monitoring.alerting.taxonomy import Severity


HL_URL = "https://api.hyperliquid.xyz/info"
SIX_MONTHS_MS = 180 * 24 * 60 * 60 * 1000

WL_PATH = Path("bots/structure/whale_list.json")

MIN_WIN_RATE = 0.60
MIN_TRADES = 20
MIN_NOTIONAL_USD = 5_000_000.0

log = get_logger(__name__)


async def _fetch(session: aiohttp.ClientSession, addr: str, since_ms: int):
    try:
        async with session.post(
            HL_URL,
            json={"type": "userFillsByTime", "user": addr, "startTime": since_ms},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status != 200:
                return ("http_error", r.status, [])
            return ("ok", r.status, await r.json())
    except Exception as e:
        return ("exception", str(e), [])


def _stats(fills):
    if not fills:
        return None
    fills = sorted(fills, key=lambda f: f.get("time", 0))
    pos = {}
    open_ts = {}
    closed = 0
    wins = 0
    cum = 0.0
    for f in fills:
        coin = f.get("coin", "")
        if not coin:
            continue
        try:
            sz = float(f.get("sz", 0) or 0)
            px = float(f.get("px", 0) or 0)
        except Exception:
            continue
        if sz == 0 or px == 0:
            continue
        side = f.get("side", "")
        ts = int(f.get("time", 0) or 0)
        signed = sz if side in ("B", "buy") else -sz
        cum += abs(sz * px)
        st = pos.setdefault(coin, {"size": 0.0, "cost": 0.0})
        prior = st["size"]
        new = prior + signed
        crossed = (prior > 0 and new <= 0) or (prior < 0 and new >= 0)
        if crossed and prior != 0:
            close_qty = min(abs(prior), abs(signed))
            avg = (st["cost"] / abs(prior)) if prior else 0.0
            pnl = ((px - avg) if prior > 0 else (avg - px)) * close_qty
            closed += 1
            if pnl > 0:
                wins += 1
            if abs(new) < 1e-9:
                st["size"] = 0
                st["cost"] = 0
                open_ts.pop(coin, None)
            else:
                st["size"] = new
                st["cost"] = abs(new) * px
                open_ts[coin] = ts
        else:
            same_dir = (prior >= 0 and signed > 0) or (prior <= 0 and signed < 0)
            if same_dir:
                st["cost"] += sz * px
                st["size"] = new
                if prior == 0:
                    open_ts[coin] = ts
            else:
                st["size"] = new
                st["cost"] = abs(new) * (st["cost"] / abs(prior)) if prior else 0
    if closed == 0:
        return None
    return {
        "win_rate": wins / closed,
        "closed_positions_6mo": closed,
        "cumulative_notional_usd": cum,
    }


async def _refresh() -> dict:
    if not WL_PATH.exists():
        return {"error": f"whale_list.json missing at {WL_PATH}"}
    wl = json.loads(WL_PATH.read_text())
    whales = wl.get("whales", [])
    if not whales:
        return {"error": "whale_list is empty — nothing to refresh"}

    since = int(time.time() * 1000) - SIX_MONTHS_MS
    log.info("whale_refresh_start", count=len(whales), since_ms=since)

    async with aiohttp.ClientSession() as s:
        results = await asyncio.gather(*(_fetch(s, w["address"], since) for w in whales))

    kept = []
    dropped = []
    fetch_errors = 0
    for w, (status, code, fills) in zip(whales, results):
        if status != "ok":
            fetch_errors += 1
            dropped.append({
                "address": w["address"],
                "reason": f"fetch_error: {status} {code}",
                "fills_count": 0,
            })
            continue
        n_fills = len(fills) if isinstance(fills, list) else 0
        st = _stats(fills)
        if st is None:
            dropped.append({
                "address": w["address"],
                "reason": "no closed positions in last 180d",
                "fills_count": n_fills,
            })
            continue
        if (
            st["win_rate"] < MIN_WIN_RATE
            or st["closed_positions_6mo"] < MIN_TRADES
            or st["cumulative_notional_usd"] < MIN_NOTIONAL_USD
        ):
            dropped.append({
                "address": w["address"],
                "reason": (
                    f"below floor: WR={st['win_rate']:.2%} "
                    f"trades={st['closed_positions_6mo']} "
                    f"notional=${st['cumulative_notional_usd']:,.0f}"
                ),
                "fills_count": n_fills,
                "current_stats": st,
            })
            continue
        kept.append({
            "address": w["address"],
            "fills_count": n_fills,
            "win_rate": round(st["win_rate"], 4),
            "closed_positions_6mo": st["closed_positions_6mo"],
            "cumulative_notional_usd": round(st["cumulative_notional_usd"], 2),
        })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "examined": len(whales),
        "kept_count": len(kept),
        "dropped_count": len(dropped),
        "fetch_errors": fetch_errors,
        "kept": kept,
        "dropped": dropped,
    }


def _suspicious(summary: dict) -> bool:
    """Heuristic: if everything would be dropped, treat as suspicious data quality
    issue rather than a real-world signal. Don't auto-action; alert as P1."""
    return (
        summary.get("examined", 0) > 0
        and summary.get("kept_count", 0) == 0
        and summary.get("dropped_count", 0) == summary.get("examined", 0)
    )


def _format_alert_body(summary: dict) -> str:
    if "error" in summary:
        return f"Refresh ERROR: {summary['error']}"
    n = summary["examined"]
    k = summary["kept_count"]
    d = summary["dropped_count"]
    fe = summary["fetch_errors"]
    lines = [
        f"Examined {n} whales: {k} still qualify, {d} would drop ({fe} fetch errors).",
    ]
    if d > 0:
        lines.append("Dropped reasons (first 5):")
        for w in summary["dropped"][:5]:
            short = w["address"][:10] + "..."
            lines.append(f"  {short}: {w['reason']}")
    if k != n:
        lines.append("")
        lines.append(
            "Action: review summary in audit_log (event_type='whale_refresh_completed'). "
            "If you concur, manually update bots/structure/whale_list.json + commit/push."
        )
    if _suspicious(summary):
        lines.append("")
        lines.append(
            "WARNING: ALL whales dropped. May indicate transient API issue rather "
            "than real degradation. Verify by re-running before acting."
        )
    return "\n".join(lines)


def main() -> None:
    configure_logging()
    summary = asyncio.run(_refresh())

    write_audit(
        "whale_refresh_completed",
        actor="cron:quarterly_whale_refresh",
        payload=summary,
    )

    severity = Severity.P1 if _suspicious(summary) else Severity.P2
    emit_alert(
        severity=severity,
        title=f"Quarterly whale refresh: kept {summary.get('kept_count', '?')}/{summary.get('examined', '?')}",
        body=_format_alert_body(summary),
        event_type="whale_refresh",
    )

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
