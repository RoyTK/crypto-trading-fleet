"""One-off data fetcher + statistics computer for the crypto correlation thesis.

Pulls public free data sources that the user's web-extension session
couldn't access (CoinGecko blocked there, accessible here), computes the
statistics the report needed but couldn't produce (rolling correlations,
event-study returns, BTC dominance trend, stablecoin lead-lag).

Run locally: python scripts/correlation_research_data.py
Output: prints structured summary to stdout for team-meeting injection.

Not part of the production fleet — research artifact.
"""
from __future__ import annotations

import json
import math
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from statistics import mean, stdev


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def fetch_json(url: str, timeout: int = 30) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (correlation-research)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def cg_range(coin_id: str, days_back: int = 365) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """CoinGecko market_chart — free tier max 365 days at daily granularity.
    Returns (prices, market_caps) as [(ts_seconds, value), ...]."""
    url = (
        f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
        f"?vs_currency=usd&days={min(days_back, 365)}&interval=daily"
    )
    raw = fetch_json(url)
    prices = [(int(p[0] / 1000), float(p[1])) for p in raw.get("prices", [])]
    mcaps = [(int(p[0] / 1000), float(p[1])) for p in raw.get("market_caps", [])]
    return prices, mcaps


def llama_stablecoin_supply() -> list[tuple[int, float]]:
    """DeFiLlama total stablecoin supply time series."""
    url = "https://stablecoins.llama.fi/stablecoincharts/all"
    raw = fetch_json(url)
    out = []
    for entry in raw:
        ts = int(entry["date"])
        peg = entry.get("totalCirculatingUSD", {})
        # peg may be a dict of {peg_type: usd}; sum to get total
        if isinstance(peg, dict):
            total = sum(float(v) for v in peg.values())
        else:
            total = float(peg)
        out.append((ts, total))
    return sorted(out)


def fred_sp500() -> list[tuple[int, float]]:
    """SP500 daily close from FRED CSV (no auth needed for SP500 series)."""
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=SP500"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        text = r.read().decode()
    out = []
    lines = text.strip().split("\n")
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            date = datetime.strptime(parts[0].strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            val_str = parts[1].strip()
            if val_str in (".", "", "NA"):
                continue
            close = float(val_str)
            out.append((int(date.timestamp()), close))
        except (ValueError, IndexError):
            continue
    return sorted(out)


def yahoo_sp500() -> list[tuple[int, float]]:
    """Fallback: SP500 from Yahoo Finance chart API."""
    end_ts = int(time.time())
    start_ts = end_ts - 760 * 86400
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"
        f"?period1={start_ts}&period2={end_ts}&interval=1d"
    )
    raw = fetch_json(url)
    result = raw.get("chart", {}).get("result", [{}])[0]
    timestamps = result.get("timestamp", [])
    closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    out = []
    for i, ts in enumerate(timestamps):
        if i < len(closes) and closes[i] is not None:
            out.append((int(ts), float(closes[i])))
    return sorted(out)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def to_daily(series: list[tuple[int, float]]) -> dict[str, float]:
    """Map series to {YYYY-MM-DD: price} taking last value per day."""
    out: dict[str, float] = {}
    for ts, p in series:
        d = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        out[d] = p
    return out


def returns(series: list[float]) -> list[float]:
    return [(series[i] - series[i - 1]) / series[i - 1] for i in range(1, len(series)) if series[i - 1] > 0]


def pearson(x: list[float], y: list[float]) -> float | None:
    n = min(len(x), len(y))
    if n < 5:
        return None
    x = x[-n:]; y = y[-n:]
    mx = mean(x); my = mean(y)
    cov = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    sx = math.sqrt(sum((xi - mx) ** 2 for xi in x))
    sy = math.sqrt(sum((yi - my) ** 2 for yi in y))
    if sx == 0 or sy == 0:
        return None
    return cov / (sx * sy)


def rolling_corr(x_dict: dict[str, float], y_dict: dict[str, float], window_days: int) -> list[tuple[str, float]]:
    common_dates = sorted(set(x_dict.keys()) & set(y_dict.keys()))
    x_seq = [x_dict[d] for d in common_dates]
    y_seq = [y_dict[d] for d in common_dates]
    x_rets = returns(x_seq)
    y_rets = returns(y_seq)
    out = []
    for i in range(window_days, len(x_rets)):
        c = pearson(x_rets[i - window_days:i], y_rets[i - window_days:i])
        if c is not None:
            out.append((common_dates[i + 1], c))
    return out


def event_study(series: dict[str, float], event_date: str, pre: int = 5, post: int = 30) -> dict:
    """Returns dict with cumulative returns at +1d, +5d, +30d post-event."""
    dates = sorted(series.keys())
    if event_date not in series:
        # find nearest after
        for d in dates:
            if d >= event_date:
                event_date = d
                break
        else:
            return {"error": "event date past data range"}
    idx = dates.index(event_date)
    px0 = series[event_date]
    out = {"event_date": event_date, "px0": px0}
    for h in [1, 5, 30]:
        if idx + h < len(dates):
            px = series[dates[idx + h]]
            out[f"ret_{h}d"] = (px - px0) / px0
        else:
            out[f"ret_{h}d"] = None
    if idx - pre >= 0:
        out["pre_5d_ret"] = (px0 - series[dates[idx - pre]]) / series[dates[idx - pre]]
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=== CRYPTO CORRELATION RESEARCH DATA ===")
    print(f"Fetched: {datetime.now(timezone.utc).isoformat()}\n")

    coins = {
        "BTC": "bitcoin",
        "ETH": "ethereum",
        "SOL": "solana",
        "XRP": "ripple",
        "DOGE": "dogecoin",
    }

    print("[1/4] Fetching CoinGecko price + market-cap series (5 coins x 365 days)...")
    prices = {}
    mcaps = {}
    for sym, cid in coins.items():
        for attempt in range(3):
            try:
                print(f"      {sym} ({cid})...", end=" ", flush=True)
                p, m = cg_range(cid, days_back=365)
                prices[sym] = to_daily(p)
                mcaps[sym] = to_daily(m)
                print(f"{len(prices[sym])} daily points, latest=${list(prices[sym].values())[-1]:.4f}")
                time.sleep(8)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    print(f"429 backoff {(attempt + 1) * 15}s...", flush=True)
                    time.sleep((attempt + 1) * 15)
                    continue
                print(f"FAIL (HTTP {e.code})")
                break
            except Exception as e:
                print(f"FAIL ({type(e).__name__}: {e})")
                break

    print("\n[2/4] Fetching SP500 (FRED, then Yahoo fallback)...")
    sp_daily = {}
    for fetcher_name, fetcher in [("FRED", fred_sp500), ("Yahoo", yahoo_sp500)]:
        try:
            sp = fetcher()
            if sp:
                sp_daily = to_daily(sp)
                latest = sorted(sp_daily.keys())[-365:]
                sp_daily = {d: sp_daily[d] for d in latest}
                print(f"      {fetcher_name}: {len(sp_daily)} daily points from {min(sp_daily)} to {max(sp_daily)}")
                break
        except Exception as e:
            print(f"      {fetcher_name} FAIL ({type(e).__name__}: {e})")

    print("\n[3/4] Fetching DeFiLlama stablecoin total supply...")
    try:
        ss = llama_stablecoin_supply()
        stable_daily = to_daily(ss)
        # truncate
        latest = sorted(stable_daily.keys())[-760:]
        stable_daily = {d: stable_daily[d] for d in latest}
        print(f"      {len(stable_daily)} daily points, latest=${stable_daily[max(stable_daily)]/1e9:.1f}B")
    except Exception as e:
        print(f"      FAIL ({type(e).__name__}: {e})")
        stable_daily = {}

    print("\n[4/4] Computing statistics...")
    print()

    # ---------------- Rolling BTC-SP500 correlation ----------------
    if "BTC" in prices and sp_daily:
        print("=== BTC vs S&P 500 — rolling 30d Pearson correlation (daily returns) ===")
        rc = rolling_corr(prices["BTC"], sp_daily, window_days=30)
        if rc:
            # Print quarterly samples
            print(f"  N data points: {len(rc)}")
            print(f"  First: {rc[0][0]}  corr30d = {rc[0][1]:.3f}")
            print(f"  Last:  {rc[-1][0]}  corr30d = {rc[-1][1]:.3f}")
            # Sample every ~60 days
            for i in range(0, len(rc), 60):
                print(f"    {rc[i][0]}: {rc[i][1]:+.3f}")
            print(f"    {rc[-1][0]}: {rc[-1][1]:+.3f}  (most recent)")
            # Range
            all_c = [c for _, c in rc]
            print(f"  Range over period: min={min(all_c):+.3f} max={max(all_c):+.3f} mean={mean(all_c):+.3f}")
            # Most recent regime
            last90 = all_c[-90:] if len(all_c) >= 90 else all_c
            print(f"  Last 90d mean correlation: {mean(last90):+.3f}")
            first90 = all_c[:90] if len(all_c) >= 180 else []
            if first90:
                print(f"  First 90d mean correlation: {mean(first90):+.3f}")
        print()

    # ---------------- BTC dominance time series ----------------
    if "BTC" in mcaps:
        print("=== BTC dominance (BTC_mcap / sum_of_top5_mcap) ===")
        common = sorted(set(mcaps["BTC"]) & set(mcaps.get("ETH", {})) & set(mcaps.get("SOL", {}))
                        & set(mcaps.get("XRP", {})) & set(mcaps.get("DOGE", {})))
        if common:
            doms = []
            for d in common:
                tot = sum(mcaps[s][d] for s in ["BTC", "ETH", "SOL", "XRP", "DOGE"])
                if tot > 0:
                    doms.append((d, mcaps["BTC"][d] / tot))
            if doms:
                print(f"  N points: {len(doms)}")
                print(f"  Earliest: {doms[0][0]}  dom = {doms[0][1]*100:.1f}%")
                # quarterly samples
                for i in range(0, len(doms), 90):
                    print(f"    {doms[i][0]}: {doms[i][1]*100:5.1f}%")
                print(f"    {doms[-1][0]}: {doms[-1][1]*100:5.1f}%  (most recent)")
                last_dom = doms[-1][1] * 100
                first_dom = doms[0][1] * 100
                print(f"  Trend over period: {first_dom:.1f}% -> {last_dom:.1f}% (Δ {last_dom - first_dom:+.1f}pp)")
        print()

    # ---------------- Stablecoin → BTC lead/lag ----------------
    if stable_daily and "BTC" in prices:
        print("=== Stablecoin total supply vs BTC price — lead/lag correlation ===")
        common = sorted(set(stable_daily.keys()) & set(prices["BTC"].keys()))
        ss_seq = [stable_daily[d] for d in common]
        btc_seq = [prices["BTC"][d] for d in common]
        ss_rets = returns(ss_seq)
        btc_rets = returns(btc_seq)
        # 30-day rolling correlation between stable supply growth and BTC
        if len(ss_rets) >= 60 and len(btc_rets) >= 60:
            # Cross-correlation with lags -7, -3, 0, +3, +7 (positive lag = stables lead BTC)
            lag_corrs = {}
            for lag in [-14, -7, -3, 0, 3, 7, 14]:
                if lag >= 0:
                    x = ss_rets[: len(ss_rets) - lag]
                    y = btc_rets[lag:]
                else:
                    x = ss_rets[-lag:]
                    y = btc_rets[: len(btc_rets) + lag]
                n = min(len(x), len(y))
                c = pearson(x[:n], y[:n])
                if c is not None:
                    lag_corrs[lag] = c
            print("  Lag (days, +ve = stables lead BTC) vs correlation with BTC daily returns:")
            for lag in sorted(lag_corrs.keys()):
                marker = " <-- best" if lag_corrs[lag] == max(lag_corrs.values()) else ""
                print(f"    lag = {lag:+3d} days:  r = {lag_corrs[lag]:+.4f}{marker}")
            print(f"  Current stable supply: ${stable_daily[max(stable_daily)]/1e9:.1f}B")
            print(f"  90d ago supply:        ${stable_daily[sorted(stable_daily.keys())[-90]]/1e9:.1f}B")
            print(f"  90d growth: {(stable_daily[max(stable_daily)] - stable_daily[sorted(stable_daily.keys())[-90]]) / stable_daily[sorted(stable_daily.keys())[-90]] * 100:+.1f}%")
        print()

    # ---------------- Event study: key dates ----------------
    if "BTC" in prices:
        print("=== Event-study returns (BTC) ===")
        events = [
            ("2024-04-19", "BTC 4th halving"),
            ("2025-01-27", "DeepSeek release"),
            ("2025-02-14", "LIBRA scandal"),
            ("2025-03-06", "Trump Strategic Reserve EO"),
            ("2025-07-17", "GENIUS Act signed"),
            ("2025-10-14", "BTC cycle high ~$126k"),
            ("2026-02-28", "Iran war begins"),
            ("2026-04-18", "rsETH exploit"),
            ("2026-05-06", "JPM/Mastercard/Ripple XRPL settlement"),
        ]
        for d, label in events:
            res = event_study(prices["BTC"], d)
            if "error" not in res:
                pre = res.get("pre_5d_ret")
                r1 = res.get("ret_1d")
                r5 = res.get("ret_5d")
                r30 = res.get("ret_30d")
                f = lambda x: f"{x*100:+6.2f}%" if x is not None else "    n/a"
                print(f"  {d} {label:40s}  pre5d={f(pre)}  +1d={f(r1)}  +5d={f(r5)}  +30d={f(r30)}")
        print()

    # ---------------- Coin drawdowns from ATH within window ----------------
    print("=== Drawdown from peak within fetched window ===")
    for sym in coins:
        if sym in prices and prices[sym]:
            pmax = max(prices[sym].values())
            pmax_date = [d for d, p in prices[sym].items() if p == pmax][0]
            pnow = prices[sym][max(prices[sym].keys())]
            dd = (pnow - pmax) / pmax
            print(f"  {sym:5s} peak ${pmax:>12,.2f} on {pmax_date}, now ${pnow:>12,.2f}, drawdown {dd*100:+6.1f}%")
    print()

    print("=== DONE ===")


if __name__ == "__main__":
    main()
