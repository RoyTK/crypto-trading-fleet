"""Pattern 6 (ETF flow regime) crux test — per the adversarial team meeting 2026-05-26.

Statistician's R2 proposal: Welch's t-test on BTC 5-day forward returns,
partitioned by binary ETF-flow regime (5d cumulative flow > +$500M = "inflow"
vs < -$500M = "outflow"). Decision threshold: Cohen's d >= 0.3 AND p < 0.0042
(Bonferroni for 12 patterns in the research thesis).

If passes → ship Pattern 6 as STRUCTURE entry gate. If fails → kill Pattern 6
permanently from the research backlog.

Data sources (Farside is 403-blocked, SoSoValue API offline at script-write
time, so this script takes a CSV path as input). Free CSV options:
- BitBo: https://bitbo.io/treasuries/etf-flows/ (page has table; export option may exist)
- The Block: https://www.theblock.co/data/etfs/bitcoin-etf/spot-bitcoin-etf-total-net-flow
- CoinGlass: https://www.coinglass.com/etf/bitcoin
- Newhedge: https://newhedge.io/bitcoin/spot-bitcoin-etf-total-net-flows

Expected CSV format: two columns
    date, net_flow_usd
    2024-01-11, 655930000
    2024-01-12, -158400000
    ...

BTC prices fetched live from CoinGecko (free, no auth).

Run:
    python scripts/pattern6_etf_flow_crux_test.py /path/to/etf_flows.csv

Or to dry-run without ETF data (uses random for sanity check):
    python scripts/pattern6_etf_flow_crux_test.py --dry-run
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
import urllib.request
from datetime import datetime, timezone
from statistics import mean, stdev


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INFLOW_THRESHOLD_USD = 500_000_000.0    # |5d cumulative flow| > $500M = regime day
COHEN_D_MIN = 0.3                       # statistician's threshold for "real effect"
BONFERRONI_ALPHA = 0.0042               # 0.05 / 12 patterns


# ---------------------------------------------------------------------------
# Statistics (stdlib only)
# ---------------------------------------------------------------------------

def welch_t_test(x: list[float], y: list[float]) -> dict:
    """Welch's two-sample t-test. Returns t, df, p (two-sided), Cohen's d."""
    nx, ny = len(x), len(y)
    if nx < 2 or ny < 2:
        return {"error": "insufficient sample"}
    mx, my = mean(x), mean(y)
    vx, vy = stdev(x) ** 2, stdev(y) ** 2
    t = (mx - my) / math.sqrt(vx / nx + vy / ny)
    df_num = (vx / nx + vy / ny) ** 2
    df_den = (vx / nx) ** 2 / (nx - 1) + (vy / ny) ** 2 / (ny - 1)
    df = df_num / df_den if df_den > 0 else 0.0
    # Pooled SD for Cohen's d
    pooled_sd = math.sqrt(((nx - 1) * vx + (ny - 1) * vy) / (nx + ny - 2))
    d = (mx - my) / pooled_sd if pooled_sd > 0 else 0.0
    # Two-sided p via Student's t survival approximation
    p = _student_t_sf(abs(t), df) * 2
    return {
        "n_inflow": nx,
        "n_outflow": ny,
        "mean_inflow_5d_ret_pct": mx * 100,
        "mean_outflow_5d_ret_pct": my * 100,
        "diff_pct": (mx - my) * 100,
        "t_stat": t,
        "df": df,
        "p_two_sided": p,
        "cohens_d": d,
    }


def _student_t_sf(t: float, df: float) -> float:
    """Survival function for Student's t (one-sided). Uses regularized
    incomplete beta function — stdlib only."""
    if df <= 0:
        return 0.5
    x = df / (df + t * t)
    return 0.5 * _incomplete_beta(x, df / 2, 0.5)


def _incomplete_beta(x: float, a: float, b: float) -> float:
    """Regularized incomplete beta function I_x(a, b). Continued-fraction
    approximation good enough for moderate df."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    bt = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log(1 - x)
    )
    if x < (a + 1) / (a + b + 2):
        return bt * _betacf(x, a, b) / a
    return 1.0 - bt * _betacf(1 - x, b, a) / b


def _betacf(x: float, a: float, b: float, max_iter: int = 200, eps: float = 1e-12) -> float:
    qab = a + b
    qap = a + 1
    qam = a - 1
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < eps:
        d = eps
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < eps:
            d = eps
        c = 1.0 + aa / c
        if abs(c) < eps:
            c = eps
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < eps:
            d = eps
        c = 1.0 + aa / c
        if abs(c) < eps:
            c = eps
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            return h
    return h


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def fetch_btc_prices(days: int = 365) -> dict[str, float]:
    """Fetch BTC daily prices from CoinGecko (free, no auth)."""
    url = f"https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days={min(days, 365)}&interval=daily"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    prices = {}
    for ts_ms, price in data.get("prices", []):
        d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        prices[d] = float(price)
    return prices


def load_etf_flows_csv(path: str) -> dict[str, float]:
    """Load (date, net_flow_usd) CSV. Date format: YYYY-MM-DD."""
    flows = {}
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        date_col = None
        flow_col = None
        for col in reader.fieldnames or []:
            if col.lower() in ("date", "day"):
                date_col = col
            elif "flow" in col.lower() or "net" in col.lower():
                flow_col = col
        if date_col is None or flow_col is None:
            raise ValueError(
                f"CSV must have a 'date' column and a column with 'flow' or 'net' in its name. "
                f"Found columns: {reader.fieldnames}"
            )
        for row in reader:
            d_raw = row[date_col].strip()
            f_raw = row[flow_col].strip().replace(",", "").replace("$", "")
            if not d_raw or not f_raw:
                continue
            try:
                # Try common date formats
                for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y", "%d/%m/%Y"):
                    try:
                        d_norm = datetime.strptime(d_raw, fmt).strftime("%Y-%m-%d")
                        break
                    except ValueError:
                        continue
                else:
                    continue
                flows[d_norm] = float(f_raw)
            except (ValueError, KeyError):
                continue
    return flows


# ---------------------------------------------------------------------------
# Crux computation
# ---------------------------------------------------------------------------

def run_crux_test(flows: dict[str, float], prices: dict[str, float]) -> dict:
    """For each date with sufficient lookback + lookforward, compute
    5d cumulative flow + BTC 5d forward return. Partition into inflow/
    outflow regime, run Welch's t."""
    sorted_dates = sorted(set(flows.keys()) & set(prices.keys()))
    if len(sorted_dates) < 30:
        return {"error": f"only {len(sorted_dates)} dates overlap between flow + price data — need >= 30"}

    inflow_returns = []
    outflow_returns = []
    skipped_neutral = 0

    for i in range(5, len(sorted_dates) - 5):
        d_today = sorted_dates[i]
        # 5d cumulative flow ending today (inclusive)
        flow_5d = sum(flows[sorted_dates[j]] for j in range(i - 4, i + 1) if sorted_dates[j] in flows)
        # 5d BTC forward return
        d_end = sorted_dates[i + 5]
        if prices[d_today] <= 0:
            continue
        ret_5d = (prices[d_end] - prices[d_today]) / prices[d_today]

        if flow_5d > INFLOW_THRESHOLD_USD:
            inflow_returns.append(ret_5d)
        elif flow_5d < -INFLOW_THRESHOLD_USD:
            outflow_returns.append(ret_5d)
        else:
            skipped_neutral += 1

    if len(inflow_returns) < 5 or len(outflow_returns) < 5:
        return {
            "error": "not enough regime days",
            "n_inflow": len(inflow_returns),
            "n_outflow": len(outflow_returns),
            "n_neutral_skipped": skipped_neutral,
        }

    result = welch_t_test(inflow_returns, outflow_returns)
    result["n_neutral_skipped"] = skipped_neutral
    result["n_total_evaluable"] = len(inflow_returns) + len(outflow_returns) + skipped_neutral
    return result


def emit_verdict(result: dict) -> str:
    """Compare against statistician's decision thresholds."""
    if "error" in result:
        return f"INCONCLUSIVE: {result['error']}"

    d = abs(result["cohens_d"])
    p = result["p_two_sided"]
    passes_d = d >= COHEN_D_MIN
    passes_p = p < BONFERRONI_ALPHA

    if passes_d and passes_p:
        verdict = "PASS — ship Pattern 6 as STRUCTURE entry gate"
    elif passes_p and not passes_d:
        verdict = "STATISTICALLY SIGNIFICANT but small effect — review with statistician before deploying"
    elif passes_d and not passes_p:
        verdict = "LARGE EFFECT but underpowered — collect more data (more regime days) before deploying"
    else:
        verdict = "FAIL — kill Pattern 6 permanently from the research backlog"
    return verdict


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("flows_csv", nargs="?",
                        help="Path to CSV with (date, net_flow_usd) columns")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip CSV requirement; sanity-check stats math with random data")
    args = parser.parse_args()

    if args.dry_run:
        print("=== DRY RUN — testing stats math with synthetic data ===\n")
        import random
        random.seed(42)
        # Synthesize: 50 inflow days with mean +0.5%, 50 outflow days with mean -0.5%
        x = [random.gauss(0.005, 0.02) for _ in range(50)]
        y = [random.gauss(-0.005, 0.02) for _ in range(50)]
        r = welch_t_test(x, y)
        print(json.dumps(r, indent=2, default=str))
        return 0

    if not args.flows_csv:
        print("ERROR: provide a CSV path with (date, net_flow_usd) columns.")
        print("Free sources (manual download — Farside/SoSoValue API are blocked):")
        print("  - https://bitbo.io/treasuries/etf-flows/")
        print("  - https://www.theblock.co/data/etfs/bitcoin-etf/spot-bitcoin-etf-total-net-flow")
        print("  - https://www.coinglass.com/etf/bitcoin")
        print("Or run with --dry-run to verify the stats math.")
        return 2

    print(f"=== PATTERN 6 ETF FLOW CRUX TEST ===")
    print(f"Run: {datetime.now(timezone.utc).isoformat()}")
    print(f"Threshold: |5d cumulative flow| > ${INFLOW_THRESHOLD_USD/1e6:.0f}M")
    print(f"Decision: pass if Cohen's d >= {COHEN_D_MIN} AND p < {BONFERRONI_ALPHA} (Bonferroni 12 tests)")
    print()

    print("[1/3] Loading ETF flows CSV...")
    try:
        flows = load_etf_flows_csv(args.flows_csv)
    except (FileNotFoundError, ValueError) as e:
        print(f"  FAIL: {e}")
        return 1
    print(f"      {len(flows)} daily flow rows, range {min(flows)} to {max(flows)}")
    print(f"      Sample: latest 3 days: {dict(list(sorted(flows.items()))[-3:])}")
    print()

    print("[2/3] Fetching BTC daily prices from CoinGecko...")
    try:
        prices = fetch_btc_prices(days=365)
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        return 1
    print(f"      {len(prices)} daily price rows")
    print()

    print("[3/3] Running Welch's t-test...")
    result = run_crux_test(flows, prices)
    print()

    print("=== RESULT ===")
    print(json.dumps(result, indent=2, default=str))
    print()

    print("=== VERDICT ===")
    print(emit_verdict(result))
    print()

    if "error" not in result:
        print("=== INTERPRETATION ===")
        print(f"Mean BTC 5d forward return on INFLOW regime days:  {result['mean_inflow_5d_ret_pct']:+.3f}%")
        print(f"Mean BTC 5d forward return on OUTFLOW regime days: {result['mean_outflow_5d_ret_pct']:+.3f}%")
        print(f"Difference: {result['diff_pct']:+.3f}%")
        print(f"Cohen's d: {abs(result['cohens_d']):.3f} (threshold {COHEN_D_MIN})")
        print(f"p-value (two-sided): {result['p_two_sided']:.5f} (threshold {BONFERRONI_ALPHA})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
