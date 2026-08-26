"""SWING-COPY validation backtest (Roy 2026-08-26, conviction fork -> pivot to swing-copy).

Thesis: on MULTI-DAY holds our ~7s-2min latency is noise, so we can follow a wallet IN and
mirror its exit OUT with real lag (unlike fresh-mint spikes where conviction died). Test it on
2 months of real wallet_swaps_log behavior + Birdeye 1H prices, BEFORE building any bot.

METHOD (per wallet, per token):
  1. pull ordered swaps; fetch Birdeye 1H history over the trade window.
  2. reconstruct token INVENTORY via qty = notional_usd / price_at(t) (segments real
     accumulate->distribute cycles; fixes the "conflated round-trip" caveat of the SQL probe).
  3. a POSITION opens when inventory rises from ~0. FOLLOW-OUT (mirror exit) fires when the
     wallet has net-distributed >= EXIT_DISTRIB_FRAC of the position's PEAK inventory. Position
     closes then (or at last event / +HOLD_CAP if never distributed -> flagged bagged).
  4. keep positions whose (open->exit) span >= MIN_SPAN_H  == the swing set.
  5. OUR trade: enter at 1H price at open_time, exit at 1H price at follow-out time.
     follow_ret = (exit_px/entry_px) - 1 - FEE_RT.
  COUNTERFACTUALS on the SAME window: (a) 30% trailing stop, (b) hold-to-forward-max (upper
     bound), (c) rug floor = min price after open within HOLD_CAP (did follow-out beat the rug?).

Outputs aggregate go/no-go + per-wallet follow edge (Track 2 roster seed) + JSON detail.
Read-only. Run inside bot_copy: docker compose exec -T bot_copy python scripts/swing_followcopy_bt.py
Env: MAX_POS (cap positions, default 700), TOP_WALLETS (default 30), FEE_RT (default 0.025).
"""
import os
import time
import json
import urllib.request
import urllib.error

from sqlalchemy import text
from framework.db import session_scope
from bots.copy.config import get_copy_settings

KEY = get_copy_settings().birdeye_api_key
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

MIN_SPAN_H       = 24.0
EXIT_DISTRIB_FRAC= 0.50          # follow-out when wallet has dumped >=50% of peak inventory
HOLD_CAP_S       = 10 * 24 * 3600
TRAIL_FRAC       = 0.30          # mechanical counterfactual: 30% trailing stop
FEE_RT           = float(os.getenv("FEE_RT", "0.025"))   # round-trip cost (~1.25%/side, audit)
MAX_POS          = int(os.getenv("MAX_POS", "700"))
TOP_WALLETS      = int(os.getenv("TOP_WALLETS", "30"))

# swing wallets: strong crude-realized, exclude hyperactive MM-like (>130 swing tokens)
WALLET_SQL = """
WITH pos AS (
  SELECT wallet_address, token_mint,
         MIN(event_at) FILTER (WHERE side='buy')  AS first_buy,
         SUM(notional_usd) FILTER (WHERE side='buy')  AS buy_usd,
         SUM(notional_usd) FILTER (WHERE side='sell') AS sell_usd,
         COUNT(*) FILTER (WHERE side='buy')  AS n_buys,
         EXTRACT(EPOCH FROM (MAX(event_at)-MIN(event_at) FILTER (WHERE side='buy')))/3600.0 AS span_h
  FROM wallet_swaps_log GROUP BY wallet_address, token_mint),
swing AS (SELECT * FROM pos WHERE first_buy IS NOT NULL AND buy_usd>=500 AND n_buys>=2
                 AND span_h>=24 AND sell_usd>=0.5*buy_usd)
SELECT wallet_address, COUNT(*) sp, ROUND((SUM(sell_usd)-SUM(buy_usd))::numeric,0) cr
FROM swing GROUP BY wallet_address
HAVING COUNT(*) BETWEEN 5 AND 130
ORDER BY cr DESC LIMIT :top
"""

SWAPS_SQL = """
SELECT EXTRACT(EPOCH FROM event_at)::bigint t, side, notional_usd
FROM wallet_swaps_log WHERE wallet_address=:w AND token_mint=:tok ORDER BY event_at
"""

# swing tokens for a wallet (same swing filter, per token)
TOKENS_SQL = """
WITH pos AS (
  SELECT token_mint,
         MIN(event_at) FILTER (WHERE side='buy')  AS first_buy,
         SUM(notional_usd) FILTER (WHERE side='buy')  AS buy_usd,
         SUM(notional_usd) FILTER (WHERE side='sell') AS sell_usd,
         COUNT(*) FILTER (WHERE side='buy')  AS n_buys,
         EXTRACT(EPOCH FROM (MAX(event_at)-MIN(event_at) FILTER (WHERE side='buy')))/3600.0 AS span_h
  FROM wallet_swaps_log WHERE wallet_address=:w GROUP BY token_mint)
SELECT token_mint FROM pos WHERE first_buy IS NOT NULL AND buy_usd>=500 AND n_buys>=2
  AND span_h>=24 AND sell_usd>=0.5*buy_usd
"""


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
                time.sleep(5); continue
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


def segment(swaps, series):
    """Reconstruct inventory & segment into positions. Returns list of dicts with
    open_t, exit_t, entry_px, exit_px, peak_before_exit, and post-window path handled by caller."""
    q = 0.0            # token inventory
    peak = 0.0         # peak inventory in the current open position
    sold_since_open = 0.0
    open_t = None
    positions = []
    for t, side, notional in swaps:
        px = price_at(series, t)
        if not px or px <= 0:
            continue
        qty = notional / px
        if side == 'buy':
            if q <= 1e-9 and open_t is None:
                open_t = t
                peak = 0.0
                sold_since_open = 0.0
            q += qty
            peak = max(peak, q)
        else:  # sell
            if open_t is None:
                continue  # sell with no tracked position (pre-window) -> ignore
            q -= qty
            sold_since_open += qty
            # follow-out: dumped >= EXIT_DISTRIB_FRAC of peak inventory
            if peak > 0 and sold_since_open >= EXIT_DISTRIB_FRAC * peak:
                positions.append(dict(open_t=open_t, exit_t=t,
                                      entry_px=price_at(series, open_t),
                                      exit_px=px, peak=peak))
                # reset (remaining dust ignored; new buy reopens)
                q = 0.0; open_t = None; peak = 0.0; sold_since_open = 0.0
    return positions


def main():
    now = int(time.time())
    with session_scope() as s:
        wallets = s.execute(text(WALLET_SQL), {"top": TOP_WALLETS}).all()
        pcount = 0
        per_wallet = {}
        detail = []
        pxcache = {}
        for w, sp, cr in wallets:
            toks = [r[0] for r in s.execute(text(TOKENS_SQL), {"w": w}).all()]
            wl = per_wallet.setdefault(w, dict(n=0, wins=0, sum_ret=0.0, follow=[], mech=[],
                                               beat_rug=0, rug_n=0, cr=float(cr)))
            for tok in toks:
                if pcount >= MAX_POS:
                    break
                swaps = [(int(t), sd, float(n)) for t, sd, n in
                         s.execute(text(SWAPS_SQL), {"w": w, "tok": tok}).all()]
                if len(swaps) < 3:
                    continue
                t0 = swaps[0][0] - 3600
                t1 = min(now, swaps[-1][0] + HOLD_CAP_S)
                if tok not in pxcache:
                    pxcache[tok] = hist_1h(tok, t0, t1)
                    time.sleep(0.15)
                series = pxcache[tok]
                if len(series) < 5:
                    continue
                for p in segment(swaps, series):
                    if not p["entry_px"] or not p["exit_px"] or p["entry_px"] <= 0:
                        continue
                    span_h = (p["exit_t"] - p["open_t"]) / 3600.0
                    if span_h < MIN_SPAN_H:
                        continue
                    pcount += 1
                    follow_ret = p["exit_px"] / p["entry_px"] - 1.0 - FEE_RT
                    # mechanical: 30% trailing stop over [open, exit+cap], forward path
                    fwd = [(u, px) for u, px in series if u >= p["open_t"]]
                    peak_px = p["entry_px"]; mech_px = None
                    for u, px in fwd:
                        if u > p["exit_t"] + HOLD_CAP_S:
                            break
                        peak_px = max(peak_px, px)
                        if px <= peak_px * (1 - TRAIL_FRAC):
                            mech_px = px; break
                    if mech_px is None:
                        mech_px = fwd[-1][1] if fwd else p["exit_px"]
                    mech_ret = mech_px / p["entry_px"] - 1.0 - FEE_RT
                    # rug floor after open within cap
                    post = [px for u, px in fwd if u <= p["open_t"] + HOLD_CAP_S]
                    rug_px = min(post) if post else p["exit_px"]
                    rug_ret = rug_px / p["entry_px"] - 1.0 - FEE_RT
                    beat_rug = follow_ret > rug_ret + 1e-9
                    wl["n"] += 1
                    wl["wins"] += 1 if follow_ret > 0 else 0
                    wl["sum_ret"] += follow_ret
                    wl["follow"].append(follow_ret)
                    wl["mech"].append(mech_ret)
                    wl["beat_rug"] += 1 if beat_rug else 0
                    wl["rug_n"] += 1
                    detail.append(dict(w=w[:8], tok=tok[:8], span_h=round(span_h, 1),
                                       follow=round(follow_ret, 4), mech=round(mech_ret, 4),
                                       rug=round(rug_ret, 4), peak=round(p["peak"], 2)))
            if pcount >= MAX_POS:
                break

    # ---- aggregate ----
    allf = [d["follow"] for d in detail]
    allm = [d["mech"] for d in detail]
    n = len(allf)
    if not n:
        print("NO positions with price coverage."); return

    def med(x):
        x = sorted(x); k = len(x)
        return (x[k // 2] if k % 2 else (x[k // 2 - 1] + x[k // 2]) / 2) if k else 0.0

    SIZE = 500.0
    follow_pnl = SIZE * sum(allf)
    mech_pnl = SIZE * sum(allm)
    wins = sum(1 for r in allf if r > 0)
    beat_rug = sum(d["follow"] > d["rug"] + 1e-9 for d in detail)

    print("=" * 74)
    print(f"SWING-COPY follow-in/follow-out backtest  (fee_rt={FEE_RT:.3f}, ${int(SIZE)}/trade)")
    print(f"positions (span>={MIN_SPAN_H:.0f}h, priced): {n}   wallets: {len(per_wallet)}")
    print("-" * 74)
    print(f"FOLLOW  mean ret {sum(allf)/n:+.2%}  median {med(allf):+.2%}  "
          f"win {wins}/{n} ({wins/n:.0%})  total ${follow_pnl:+,.0f}")
    print(f"MECH30  mean ret {sum(allm)/n:+.2%}  median {med(allm):+.2%}  total ${mech_pnl:+,.0f}")
    print(f"follow beats rug-floor: {beat_rug}/{n} ({beat_rug/n:.0%})")
    print("=" * 74)
    print("PER-WALLET follow edge (Track 2 roster seed), by total follow $:")
    rows = []
    for w, d in per_wallet.items():
        if d["n"] == 0:
            continue
        tot = SIZE * d["sum_ret"]
        rows.append((tot, w, d))
    for tot, w, d in sorted(rows, reverse=True):
        print(f"  {w[:8]}  n={d['n']:3d}  win {d['wins']}/{d['n']:<3d}  "
              f"mean {d['sum_ret']/d['n']:+.1%}  total ${tot:+,.0f}  crude_real ${d['cr']:+,.0f}")

    out = dict(params=dict(min_span_h=MIN_SPAN_H, exit_frac=EXIT_DISTRIB_FRAC, fee_rt=FEE_RT,
                           trail=TRAIL_FRAC, size=SIZE),
               agg=dict(n=n, wallets=len(per_wallet), follow_mean=sum(allf)/n,
                        follow_median=med(allf), follow_win=wins/n, follow_pnl=follow_pnl,
                        mech_pnl=mech_pnl, beat_rug=beat_rug/n),
               per_wallet={w[:8]: dict(n=d["n"], wins=d["wins"],
                                       mean_ret=(d["sum_ret"]/d["n"] if d["n"] else 0),
                                       total=SIZE*d["sum_ret"], crude_real=d["cr"])
                           for w, d in per_wallet.items() if d["n"]},
               detail=detail)
    with open("/tmp/swing_bt.json", "w") as f:
        json.dump(out, f, indent=1)
    print("\ndetail -> /tmp/swing_bt.json")


if __name__ == "__main__":
    main()
