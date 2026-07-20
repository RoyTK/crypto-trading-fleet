"""Counterfactual PnL: what if conviction HELD (cohort gate) instead of exiting on the
single trigger wallet's sell — and does WIDENING the hold window 20->60min matter?
(Roy 2026-07-19)

For each conviction `trigger_wallet_exit` trade we compute the cohort net-flow (net buy-sell
USD of OTHER subscribed wallets, trailing W min) at 20 and 60 min. Where the gate would HOLD
(netW > 0) we replay the held position forward and compare its exit PnL to what we actually
banked at the trigger sell.

HELD-EXIT RULE (faithful to the live feature: hold while the crowd accumulates, exit when it
leaves): walk forward wallet_swaps_log; exit when the trailing-60min forward cohort net-flow
first turns <= 0 (crowd left), OR a 25% hard stop vs the forward 1H path, OR a 48h timeout —
whichever first. Price at the exit time from Birdeye 1H history.

DELTA per trade = size_usd * (held_exit_price - actual_exit_price) / entry_price  — the extra
PnL from holding longer on the SAME position (round-trip fee/slippage ~cancels in the delta,
so this is a gross price-delta estimate). Also reports a hold-to-forward-MAX upper bound.

CAVEAT (stated, not hidden): this treats each exit INDEPENDENTLY. On a multi-trade token,
holding exit_i would also suppress the later re-entries (has_open_position) — this sim does
NOT remove those (a full sequential sim would, generally FAVORING holding by cutting churn
losses). So the per-exit delta is directional. Read-only; runs in bot_copy.
"""
import time
import json
import urllib.request
import urllib.error

from sqlalchemy import text
from framework.db import session_scope
from bots.copy.config import get_copy_settings

KEY = get_copy_settings().birdeye_api_key
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
STOP_FRAC = 0.75          # conviction 25% hard stop
TIMEOUT_S = 48 * 3600
TURN_WIN = 3600           # trailing 60min window for the forward cohort-turn


def _be(path, tries=3):
    for _ in range(tries):
        try:
            r = urllib.request.Request(
                "https://public-api.birdeye.so" + path,
                headers={"X-API-KEY": KEY, "x-chain": "solana",
                         "Accept": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(r, timeout=25) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5)
                continue
            return None
        except Exception:
            time.sleep(2)
    return None


def hist_1h(mint, t_from, t_to):
    h = _be(f"/defi/history_price?address={mint}&address_type=token&type=1H"
            f"&time_from={t_from}&time_to={t_to}")
    items = ((h or {}).get("data") or {}).get("items") or []
    return sorted([(int(it["unixTime"]), float(it["value"]))
                   for it in items if it.get("value")], key=lambda x: x[0])


def price_at(series, t):
    prev = None
    for u, p in series:
        if u <= t:
            prev = p
        else:
            break
    return prev if prev is not None else (series[0][1] if series else None)


TRADES_SQL = """
SELECT id, asset, entry_price, exit_price, size_usd, pnl_usd,
       EXTRACT(EPOCH FROM exit_at)::bigint AS exit_unix,
       sim_metadata->>'trigger_wallet' AS trig
FROM trades
WHERE bot_id='copy' AND mode='paper' AND sim_metadata->>'strategy'='conviction'
  AND fill_status='closed' AND exit_reason='trigger_wallet_exit'
  AND exit_price>0 AND entry_price>0 AND exit_at IS NOT NULL
ORDER BY asset, exit_at
"""


def cohort_net(s, token, trig, lo, hi):
    v = s.execute(text(
        "SELECT COALESCE(SUM(CASE WHEN side='buy' THEN notional_usd ELSE -notional_usd END),0) "
        "FROM wallet_swaps_log WHERE token_mint=:tok "
        "AND event_at > to_timestamp(:lo) AND event_at <= to_timestamp(:hi) "
        "AND (:trig IS NULL OR wallet_address<>:trig)"),
        {"tok": token, "lo": lo, "hi": hi, "trig": trig}).scalar()
    return float(v or 0.0)


def fwd_events(s, token, trig, t0, t1):
    rows = s.execute(text(
        "SELECT EXTRACT(EPOCH FROM event_at)::bigint t, "
        "CASE WHEN side='buy' THEN notional_usd ELSE -notional_usd END sn "
        "FROM wallet_swaps_log WHERE token_mint=:tok "
        "AND event_at > to_timestamp(:t0) AND event_at <= to_timestamp(:t1) "
        "AND (:trig IS NULL OR wallet_address<>:trig) ORDER BY event_at"),
        {"tok": token, "trig": trig, "t0": t0, "t1": t1}).all()
    return [(int(a), float(b)) for a, b in rows]


def held_exit(series, events, exit_unix, entry):
    """Return (held_exit_price, reason). Hold to first forward trailing-60m cohort<=0,
    else 25% stop, else 48h timeout."""
    t_timeout = exit_unix + TIMEOUT_S
    t_turn = t_timeout
    for t, _sn in events:
        net = sum(s for (tt, s) in events if t - TURN_WIN < tt <= t)
        if net <= 0:
            t_turn = t
            break
    stop_px = entry * STOP_FRAC
    for u, p in series:
        if u > min(t_turn, t_timeout):
            break
        if u >= exit_unix and p <= stop_px:
            return stop_px, "stop"
    t_exit = min(t_turn, t_timeout)
    px = price_at(series, t_exit)
    return px, ("cohort_turn" if t_turn < t_timeout else "timeout")


def main():
    now = int(time.time())
    with session_scope() as s:
        trades = [dict(r) for r in s.execute(text(TRADES_SQL)).mappings()]
        out = []
        for t in trades:
            eu = int(t["exit_unix"])
            entry, axp = float(t["entry_price"]), float(t["exit_price"])
            size, apnl, trig, asset = float(t["size_usd"]), float(t["pnl_usd"]), t["trig"], t["asset"]
            n20 = cohort_net(s, asset, trig, eu - 1200, eu)
            n60 = cohort_net(s, asset, trig, eu - 3600, eu)
            series = hist_1h(asset, eu - 600, min(now, eu + TIMEOUT_S))
            time.sleep(0.2)
            fwd = [p for u, p in series if u >= eu]
            fmax = max(fwd) if fwd else axp
            ev = fwd_events(s, asset, trig, eu, eu + TIMEOUT_S)
            hpx, hreason = held_exit(series, ev, eu, entry)
            d_model = size * (hpx - axp) / entry if hpx else 0.0
            d_max = size * (fmax - axp) / entry
            out.append(dict(id=t["id"], tok=asset[:5], n20=n20, n60=n60, apnl=apnl,
                            d_model=d_model, d_max=d_max, hreason=hreason,
                            hold20=n20 > 0, hold60=n60 > 0))

    base = sum(r["apnl"] for r in out)
    d20 = sum(r["d_model"] for r in out if r["hold20"])
    d60 = sum(r["d_model"] for r in out if r["hold60"])
    dmax60 = sum(r["d_max"] for r in out if r["hold60"])
    incr = [r for r in out if r["hold60"] and not r["hold20"]]
    d_incr = sum(r["d_model"] for r in incr)

    print(f"conviction trigger_wallet_exit trades: {len(out)}")
    print(f"baseline realized PnL (as-is):            ${base:,.2f}")
    print(f"hold@20min gate (model exit):             ${base + d20:,.2f}   (delta {d20:+,.2f})")
    print(f"hold@60min gate (model exit):             ${base + d60:,.2f}   (delta {d60:+,.2f})")
    print(f"  -> WIDENING 20->60min incremental delta: {d_incr:+,.2f}  over {len(incr)} newly-held trades")
    print(f"hold@60min UPPER BOUND (sell at fwd-max):  ${base + dmax60:,.2f}   (delta {dmax60:+,.2f})")
    print()
    print("held trades (n60>0) — id, tok, net20, net60, actual_pnl, model_delta, upper(max_delta), held_exit:")
    for r in sorted((r for r in out if r["hold60"]), key=lambda r: -r["d_model"]):
        tag = "  <-- NEW at 60min" if not r["hold20"] else ""
        print(f"  {r['id']}  {r['tok']:6} n20={r['n20']:8.0f} n60={r['n60']:8.0f} "
              f"apnl={r['apnl']:8.2f} d_model={r['d_model']:8.2f} d_max={r['d_max']:8.2f} "
              f"{r['hreason']}{tag}")


if __name__ == "__main__":
    main()
