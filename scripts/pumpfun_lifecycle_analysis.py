"""Lifecycle analysis: pump.fun tokens over their first 60 days from mint.

Curiosity-driven research per Roy 2026-05-30. Answers:
  - Distribution of high/low (how many 2x / 10x / 100x / 1000x)
  - Time-to-peak and post-peak decay
  - Bounce patterns (did it recover from a dump?)
  - Typical price-path SHAPES via k-means clustering on normalized paths
  - Survivor (didn't die) vs rug (lost >95% within first week) comparison

Default window: tokens minted 2026-03-01 to 2026-03-15 (so the full 60d
lifecycle is observable through ~2026-05-15). Sample size default 20
(pilot) to validate data sources before scaling to 200.

Usage:

  # Pilot (20 tokens) — validates discovery + fetcher path
  docker compose exec bot_copy python -m scripts.pumpfun_lifecycle_analysis

  # Full sample
  docker compose exec bot_copy python -m scripts.pumpfun_lifecycle_analysis --sample-size 200

  # Different window
  docker compose exec bot_copy python -m scripts.pumpfun_lifecycle_analysis \
      --from 2026-02-01 --to 2026-02-15 --sample-size 100

Outputs:
  /tmp/pumpfun_lifecycle_<timestamp>.md  — full markdown report
  /tmp/pumpfun_lifecycle_<timestamp>.csv — per-token features for follow-up

This script is research, NOT a bot. Read-only against Birdeye + DexScreener.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import statistics
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

BIRDEYE_BASE = "https://public-api.birdeye.so"
DEXSCREENER_BASE = "https://api.dexscreener.com"
# v3 is the live endpoint (the original frontend-api.pump.fun host's DNS is dead
# per 2026-05-31 probe). v3 has a 10k offset cap and silently ignores any
# date-range filter params — must paginate from newest with sort/order.
PUMPFUN_FRONTEND_BASE = "https://frontend-api-v3.pump.fun"

# Birdeye history_price caps at 1000 points per call. 60d at 4H = 360 points.
HISTORY_RESOLUTION = "4H"
HISTORY_DAYS = 60

# Rate limits (Birdeye Lite tier: ~100 RPS, but be polite)
BIRDEYE_SLEEP_SECONDS = 0.6
DEXSCREENER_SLEEP_SECONDS = 0.4
PUMPFUN_SLEEP_SECONDS = 0.6

# Browser User-Agent — the original UA was blocked by upstream WAF.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)

# pump.fun v3 offset cap. Empirically 10000 returns []; we stop pagination
# earlier to leave headroom for partial pages.
PUMPFUN_OFFSET_CAP = 9500


# ----------------------------------------------------------------------------
# Types
# ----------------------------------------------------------------------------

@dataclass
class TokenMeta:
    mint: str
    symbol: Optional[str] = None
    name: Optional[str] = None
    created_at: Optional[datetime] = None
    discovery_source: str = "unknown"
    graduated: bool = False  # `complete` flag from pump.fun v3 = made it to PumpSwap
    extra: dict = field(default_factory=dict)


@dataclass
class Candle:
    ts: datetime
    price_usd: float


@dataclass
class TokenFeatures:
    mint: str
    symbol: Optional[str]
    n_candles: int
    entry_price: float
    final_price: float
    max_price: float
    min_price: float
    # Multiples vs entry (the FIRST candle's price)
    max_multiple: float
    min_multiple: float
    final_multiple: float
    # Timing
    days_to_peak: float
    days_to_trough: float
    # Patterns
    graduated: bool       # made it to PumpSwap (per pump.fun v3 `complete` flag)
    rugged: bool          # lost >95% within first 7 days
    survived_60d: bool    # final price > 10% of entry
    had_bounce: bool      # min was hit BEFORE max (recovery pattern)
    multi_peak: bool      # ≥2 local peaks within 80% of global max
    # Normalized path for clustering (30 points, log-scaled vs entry)
    normalized_path: list[float] = field(default_factory=list)


# ----------------------------------------------------------------------------
# HTTP utility
# ----------------------------------------------------------------------------

def _http_get_json(url: str, headers: Optional[dict] = None, timeout: int = 20) -> Optional[dict]:
    req = urllib.request.Request(url, headers={**(headers or {}), "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode())
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Discovery: try multiple sources, take what we can get
# ----------------------------------------------------------------------------

def discover_via_pumpfun_api(
    from_dt: datetime, to_dt: datetime, target_count: int,
    graduated_only: bool = True,
    sort_order: str = "DESC",
) -> list[TokenMeta]:
    """pump.fun v3 coins endpoint discovery.

    Probe verdict 2026-05-31 + first-run 2026-05-31:
    - v3 base URL: https://frontend-api-v3.pump.fun (original DNS dead)
    - Date-range filter params (start_time, before_timestamp, created_after)
      are SILENTLY IGNORED
    - sort=created_timestamp&order=ASC|DESC works
    - complete=true filters to graduated tokens; effective cap is ~1050
      (lower than the ~10000 default cap because the filter pool is smaller)
    - DESC + complete=true reaches ~7-14 days back from today
    - ASC + complete=true starts from earliest graduates (~Jan 2024)
      and covers ~3-6 months forward of pump.fun's earliest era

    Strategy: paginate in the chosen sort direction. Track timestamps of
    skipped tokens so we can tell the user what window was actually reachable
    if the requested window misses.

    Use DESC for "recent tokens" research (last ~14 days).
    Use ASC for "first 60 days of pump.fun's earliest graduates" research
    (gives the cleanest long-window lifecycle data since those tokens have
    years of post-mint price history).
    """
    sort_order = sort_order.upper()
    assert sort_order in ("ASC", "DESC")

    out: list[TokenMeta] = []
    seen_mints: set[str] = set()
    offset = 0
    page_size = 50
    pages_tried = 0
    skipped_count = 0
    earliest_seen: Optional[datetime] = None
    latest_seen: Optional[datetime] = None
    boundary_hit = False

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    while offset < PUMPFUN_OFFSET_CAP and len(out) < target_count:
        params = {
            "offset": str(offset),
            "limit": str(page_size),
            "sort": "created_timestamp",
            "order": sort_order,
        }
        if graduated_only:
            params["complete"] = "true"
        url = f"{PUMPFUN_FRONTEND_BASE}/coins?{urllib.parse.urlencode(params)}"
        data = _http_get_json(url, headers=headers)
        pages_tried += 1

        # Distinguish API-error (None) from end-of-data (empty list).
        if data is None:
            print(f"  [discover] page {pages_tried} (offset {offset}): "
                  f"HTTP error / non-JSON response", file=sys.stderr)
            break
        items = data if isinstance(data, list) else (data.get("data") or [])
        if not items:
            print(f"  [discover] page {pages_tried} (offset {offset}): "
                  f"API returned empty list (effective cap for this filter)",
                  file=sys.stderr)
            break

        for it in items:
            mint = str(it.get("mint") or it.get("address") or "").strip()
            if not mint or mint in seen_mints:
                continue
            ts_ms = it.get("created_timestamp") or 0
            try:
                created_at = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                continue

            # Track range of timestamps the API exposed (for diagnostics)
            if earliest_seen is None or created_at < earliest_seen:
                earliest_seen = created_at
            if latest_seen is None or created_at > latest_seen:
                latest_seen = created_at

            in_window = (from_dt <= created_at <= to_dt)

            if sort_order == "DESC":
                # Newest-first: above window → skip; below window → stop
                if created_at > to_dt:
                    skipped_count += 1
                    continue
                if created_at < from_dt:
                    boundary_hit = True
                    print(f"  [discover] hit token older than window "
                          f"({created_at.date()} < {from_dt.date()}); stopping pagination",
                          file=sys.stderr)
                    break
            else:
                # ASC (oldest-first): below window → skip; above window → stop
                if created_at < from_dt:
                    skipped_count += 1
                    continue
                if created_at > to_dt:
                    boundary_hit = True
                    print(f"  [discover] hit token newer than window "
                          f"({created_at.date()} > {to_dt.date()}); stopping pagination",
                          file=sys.stderr)
                    break

            if not in_window:
                continue  # defensive (shouldn't reach here)

            seen_mints.add(mint)
            out.append(TokenMeta(
                mint=mint,
                symbol=it.get("symbol"),
                name=it.get("name"),
                created_at=created_at,
                discovery_source="pumpfun_v3_api",
                graduated=bool(it.get("complete")),
                extra={
                    "creator": it.get("creator"),
                    "bonding_curve": it.get("bonding_curve"),
                    "raydium_pool": it.get("raydium_pool"),
                    "virtual_sol_reserves": it.get("virtual_sol_reserves"),
                },
            ))
            if len(out) >= target_count:
                return out

        if boundary_hit:
            break
        if pages_tried % 5 == 0:
            print(f"  [discover] page {pages_tried} (offset {offset}): "
                  f"{len(out)} in window, {skipped_count} skipped, "
                  f"current ts: {created_at.date() if 'created_at' in locals() else '?'}",
                  file=sys.stderr)
        offset += page_size
        time.sleep(PUMPFUN_SLEEP_SECONDS)

    # End-of-loop diagnostic — always print the range so user knows what's reachable
    if earliest_seen and latest_seen:
        print(f"  [discover] API range seen this run: "
              f"{earliest_seen.date()} → {latest_seen.date()} "
              f"({pages_tried} pages, {offset} offset reached)",
              file=sys.stderr)
        if not out:
            direction_word = "more recent" if sort_order == "DESC" else "earlier"
            print(f"  [discover] HINT: requested window {from_dt.date()} → {to_dt.date()} "
                  f"was not reached. Try a window within the API-exposed range, "
                  f"or use a {direction_word} window. "
                  f"E.g.: --from {earliest_seen.date()} --to {latest_seen.date()}",
                  file=sys.stderr)
    return out


def discover_via_birdeye_tokenlist(
    from_dt: datetime, to_dt: datetime, target_count: int,
) -> list[TokenMeta]:
    """Birdeye token list — filter by source if available, else by creation date."""
    api_key = os.environ.get("BIRDEYE_API_KEY")
    if not api_key:
        return []
    out: list[TokenMeta] = []
    seen: set[str] = set()
    offset = 0
    page_size = 50
    headers = {"X-API-KEY": api_key, "x-chain": "solana", "Accept": "application/json"}
    # Try /defi/v3/token/list_v3 — supports source + date filtering on recent versions
    while len(out) < target_count and offset < 10_000:
        params = {
            "sort_by": "created_time",
            "sort_type": "asc",
            "source": "pumpfun",
            "offset": str(offset),
            "limit": str(page_size),
            "from_time": str(int(from_dt.timestamp())),
            "to_time": str(int(to_dt.timestamp())),
        }
        url = f"{BIRDEYE_BASE}/defi/v3/token/list_v3?{urllib.parse.urlencode(params)}"
        data = _http_get_json(url, headers=headers)
        if not data:
            break
        items = (data.get("data") or {}).get("items") or []
        if not items:
            break
        for it in items:
            mint = (it.get("address") or it.get("mint") or "").strip()
            if not mint or mint in seen:
                continue
            ts = it.get("createdTime") or it.get("created_time") or 0
            try:
                created_at = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                created_at = None
            if created_at and (created_at < from_dt or created_at > to_dt):
                continue
            seen.add(mint)
            out.append(TokenMeta(
                mint=mint, symbol=it.get("symbol"), name=it.get("name"),
                created_at=created_at, discovery_source="birdeye_tokenlist",
            ))
            if len(out) >= target_count:
                return out
        offset += page_size
        time.sleep(BIRDEYE_SLEEP_SECONDS)
    return out


def discover_tokens(
    from_dt: datetime, to_dt: datetime, target_count: int,
    graduated_only: bool = True, sort_order: str = "DESC",
) -> list[TokenMeta]:
    """Try pumpfun_v3 discovery; fall back to Birdeye if it yields too few."""
    grad_str = "graduated only" if graduated_only else "ALL (incl. non-graduated)"
    print(f"[discover] pumpfun_v3 ({from_dt.date()} → {to_dt.date()}, "
          f"{grad_str}, sort={sort_order}, target {target_count})...", file=sys.stderr)
    tokens = discover_via_pumpfun_api(
        from_dt, to_dt, target_count,
        graduated_only=graduated_only, sort_order=sort_order,
    )
    if len(tokens) >= max(5, target_count // 4):
        print(f"[discover] pumpfun_v3 yielded {len(tokens)} tokens "
              f"({sum(1 for t in tokens if t.graduated)} graduated)", file=sys.stderr)
        return tokens
    print(f"[discover] pumpfun_v3 too sparse ({len(tokens)}); trying Birdeye fallback",
          file=sys.stderr)
    more = discover_via_birdeye_tokenlist(from_dt, to_dt, target_count - len(tokens))
    tokens.extend(more)
    print(f"[discover] total after fallback: {len(tokens)}", file=sys.stderr)
    return tokens


# ----------------------------------------------------------------------------
# History fetch via Birdeye
# ----------------------------------------------------------------------------

def fetch_history_birdeye(
    mint: str, from_dt: datetime, days: int = HISTORY_DAYS,
    resolution: str = HISTORY_RESOLUTION,
) -> list[Candle]:
    api_key = os.environ.get("BIRDEYE_API_KEY")
    if not api_key:
        return []
    to_dt = from_dt + timedelta(days=days)
    params = {
        "address": mint, "address_type": "token", "type": resolution,
        "time_from": str(int(from_dt.timestamp())),
        "time_to": str(int(to_dt.timestamp())),
    }
    url = f"{BIRDEYE_BASE}/defi/history_price?{urllib.parse.urlencode(params)}"
    headers = {"X-API-KEY": api_key, "x-chain": "solana", "Accept": "application/json"}
    data = _http_get_json(url, headers=headers)
    if not data:
        return []
    items = (data.get("data") or {}).get("items") or []
    out: list[Candle] = []
    for it in items:
        try:
            ts = datetime.fromtimestamp(int(it["unixTime"]), tz=timezone.utc)
            px = float(it["value"])
            if px > 0:
                out.append(Candle(ts=ts, price_usd=px))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda c: c.ts)
    return out


# ----------------------------------------------------------------------------
# Per-token feature extraction
# ----------------------------------------------------------------------------

def compute_features(
    mint: str, symbol: Optional[str], candles: list[Candle],
    graduated: bool = False,
) -> Optional[TokenFeatures]:
    if len(candles) < 5:
        return None
    entry = candles[0].price_usd
    final = candles[-1].price_usd
    prices = [c.price_usd for c in candles]
    times = [c.ts for c in candles]

    p_max = max(prices)
    p_min = min(prices)
    idx_max = prices.index(p_max)
    idx_min = prices.index(p_min)

    span_hours = max((times[-1] - times[0]).total_seconds() / 3600.0, 1.0)
    days_to_peak = (times[idx_max] - times[0]).total_seconds() / 86400.0
    days_to_trough = (times[idx_min] - times[0]).total_seconds() / 86400.0

    # 7-day rug check
    rugged = False
    seven_d_cutoff = times[0] + timedelta(days=7)
    for c in candles:
        if c.ts > seven_d_cutoff:
            break
        if c.price_usd < entry * 0.05:
            rugged = True
            break

    survived_60d = final >= entry * 0.10
    had_bounce = idx_min < idx_max and (p_max / max(p_min, 1e-18)) >= 2.0

    # Multi-peak detection: count local maxima within 80% of global max
    local_peaks = 0
    threshold = p_max * 0.80
    for i in range(1, len(prices) - 1):
        if prices[i] >= threshold and prices[i] > prices[i - 1] and prices[i] > prices[i + 1]:
            local_peaks += 1
    multi_peak = local_peaks >= 2

    # Normalize path to 30 evenly-spaced points, log-scaled vs entry
    normalized_path = _normalize_path(prices, target_len=30, entry=entry)

    return TokenFeatures(
        mint=mint, symbol=symbol, n_candles=len(candles),
        entry_price=entry, final_price=final,
        max_price=p_max, min_price=p_min,
        max_multiple=p_max / entry, min_multiple=p_min / entry,
        final_multiple=final / entry,
        days_to_peak=days_to_peak, days_to_trough=days_to_trough,
        graduated=graduated, rugged=rugged, survived_60d=survived_60d,
        had_bounce=had_bounce, multi_peak=multi_peak,
        normalized_path=normalized_path,
    )


def _normalize_path(prices: list[float], target_len: int, entry: float) -> list[float]:
    """Resample to target_len evenly-spaced indices, take log of (price/entry).

    Log scale makes 10x and 0.1x equidistant from 1.0, which is the right
    metric for shape clustering on power-law assets.
    """
    if not prices or entry <= 0:
        return [0.0] * target_len
    n = len(prices)
    if n == target_len:
        sample = prices
    else:
        sample = []
        for i in range(target_len):
            idx = int(round(i * (n - 1) / max(target_len - 1, 1)))
            sample.append(prices[idx])
    return [math.log(max(p / entry, 1e-9)) for p in sample]


# ----------------------------------------------------------------------------
# Aggregation + reporting
# ----------------------------------------------------------------------------

MULTIPLE_BINS = [
    (0.001, "<0.001x  (effectively zero)"),
    (0.01, "0.001x–0.01x  (-99%)"),
    (0.1, "0.01x–0.1x  (-90%)"),
    (0.5, "0.1x–0.5x  (-50% to -90%)"),
    (1.0, "0.5x–1x  (loss)"),
    (2.0, "1x–2x  (small gain)"),
    (10.0, "2x–10x"),
    (100.0, "10x–100x"),
    (1000.0, "100x–1000x"),
    (math.inf, "1000x+"),
]


def _bin_label(mult: float) -> str:
    for cap, label in MULTIPLE_BINS:
        if mult < cap:
            return label
    return MULTIPLE_BINS[-1][1]


def aggregate(features: list[TokenFeatures]) -> dict:
    n = len(features)
    if n == 0:
        return {"n": 0}

    max_mults = [f.max_multiple for f in features]
    final_mults = [f.final_multiple for f in features]
    min_mults = [f.min_multiple for f in features]
    days_to_peak = [f.days_to_peak for f in features]

    def _pct(arr: list[float], p: float) -> float:
        if not arr:
            return 0.0
        s = sorted(arr)
        idx = max(0, min(len(s) - 1, int(p * (len(s) - 1))))
        return s[idx]

    def _hist(arr: list[float]) -> dict[str, int]:
        out: dict[str, int] = {label: 0 for _, label in MULTIPLE_BINS}
        for v in arr:
            out[_bin_label(v)] += 1
        return out

    return {
        "n": n,
        "max_multiple": {
            "p10": _pct(max_mults, 0.10), "p25": _pct(max_mults, 0.25),
            "p50": _pct(max_mults, 0.50), "p75": _pct(max_mults, 0.75),
            "p90": _pct(max_mults, 0.90), "p99": _pct(max_mults, 0.99),
            "max": max(max_mults), "mean": statistics.mean(max_mults),
        },
        "final_multiple": {
            "p10": _pct(final_mults, 0.10), "p25": _pct(final_mults, 0.25),
            "p50": _pct(final_mults, 0.50), "p75": _pct(final_mults, 0.75),
            "p90": _pct(final_mults, 0.90), "p99": _pct(final_mults, 0.99),
            "max": max(final_mults), "mean": statistics.mean(final_mults),
        },
        "min_multiple": {
            "p10": _pct(min_mults, 0.10), "p50": _pct(min_mults, 0.50),
            "p90": _pct(min_mults, 0.90),
        },
        "days_to_peak": {
            "p10": _pct(days_to_peak, 0.10), "p50": _pct(days_to_peak, 0.50),
            "p90": _pct(days_to_peak, 0.90), "mean": statistics.mean(days_to_peak),
        },
        "max_multiple_histogram": _hist(max_mults),
        "final_multiple_histogram": _hist(final_mults),
        "counts": {
            "2x_or_more_max":    sum(1 for v in max_mults if v >= 2),
            "10x_or_more_max":   sum(1 for v in max_mults if v >= 10),
            "100x_or_more_max":  sum(1 for v in max_mults if v >= 100),
            "1000x_or_more_max": sum(1 for v in max_mults if v >= 1000),
            "10x_or_more_final":   sum(1 for v in final_mults if v >= 10),
            "100x_or_more_final":  sum(1 for v in final_mults if v >= 100),
            "1000x_or_more_final": sum(1 for v in final_mults if v >= 1000),
            "graduated":         sum(1 for f in features if f.graduated),
            "rugged_in_7d":      sum(1 for f in features if f.rugged),
            "survived_60d":      sum(1 for f in features if f.survived_60d),
            "had_bounce":        sum(1 for f in features if f.had_bounce),
            "multi_peak":        sum(1 for f in features if f.multi_peak),
        },
    }


# ----------------------------------------------------------------------------
# Path clustering (lightweight k-means without numpy dependency on fail)
# ----------------------------------------------------------------------------

def cluster_paths(features: list[TokenFeatures], k: int = 5, max_iter: int = 50) -> dict:
    """Cluster normalized paths into k shapes; return centroid + member tokens."""
    try:
        import numpy as np
        from sklearn.cluster import KMeans
    except ImportError:
        return {"available": False, "reason": "numpy/sklearn not in container"}
    if len(features) < k:
        return {"available": False, "reason": f"only {len(features)} tokens, need ≥{k}"}
    X = np.array([f.normalized_path for f in features])
    km = KMeans(n_clusters=k, n_init=10, max_iter=max_iter, random_state=42)
    labels = km.fit_predict(X)
    clusters: dict[int, dict] = {}
    for i, f in enumerate(features):
        c = int(labels[i])
        bucket = clusters.setdefault(c, {"members": [], "size": 0, "final_mults": []})
        bucket["members"].append({"mint": f.mint, "symbol": f.symbol,
                                   "max": f.max_multiple, "final": f.final_multiple})
        bucket["size"] += 1
        bucket["final_mults"].append(f.final_multiple)
    centroids = {int(i): [float(v) for v in km.cluster_centers_[i]]
                 for i in range(k)}
    # Attach centroid summary
    for c, info in clusters.items():
        info["centroid_log"] = centroids[c]
        # Interpret centroid: max log-multiple, final log-multiple, monotonicity
        peak_idx = int(np.argmax(centroids[c]))
        info["centroid_peak_idx"] = peak_idx
        info["centroid_peak_log_mult"] = centroids[c][peak_idx]
        info["centroid_final_log_mult"] = centroids[c][-1]
        info["median_final_mult"] = statistics.median(info["final_mults"])
        del info["final_mults"]
    return {"available": True, "k": k, "clusters": clusters}


# ----------------------------------------------------------------------------
# Survivor vs rug comparison
# ----------------------------------------------------------------------------

def compare_survivor_vs_rug(features: list[TokenFeatures]) -> dict:
    survivors = [f for f in features if f.survived_60d]
    ruggers = [f for f in features if f.rugged]
    others = [f for f in features if not f.survived_60d and not f.rugged]

    def _summary(group: list[TokenFeatures]) -> dict:
        if not group:
            return {"n": 0}
        return {
            "n": len(group),
            "median_max_multiple": statistics.median(f.max_multiple for f in group),
            "median_final_multiple": statistics.median(f.final_multiple for f in group),
            "median_days_to_peak": statistics.median(f.days_to_peak for f in group),
            "pct_with_bounce": 100.0 * sum(1 for f in group if f.had_bounce) / len(group),
            "pct_multi_peak": 100.0 * sum(1 for f in group if f.multi_peak) / len(group),
        }

    return {
        "survivor": _summary(survivors),
        "rug_within_7d": _summary(ruggers),
        "intermediate": _summary(others),
    }


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------

def render_markdown(report: dict) -> str:
    lines: list[str] = []
    lines.append(f"# Pump.fun token lifecycle analysis\n")
    lines.append(f"- Generated: {report['generated_at']}")
    lines.append(f"- Sample window: {report['from_dt']} → {report['to_dt']}")
    lines.append(f"- Sample size (analyzed): **N = {report['n_analyzed']}** "
                 f"(discovered {report['n_discovered']}, fetched {report['n_with_history']}, "
                 f"excluded {report['n_excluded']} for insufficient candles)")
    lines.append(f"- Discovery sources: {', '.join(report['discovery_sources'])}\n")

    agg = report['aggregate']
    if agg.get('n', 0) == 0:
        lines.append("**No tokens analyzed — likely an API or window issue.**\n")
        return "\n".join(lines)

    lines.append("## Headline distribution\n")
    counts = agg['counts']
    n = agg['n']
    lines.append(f"- **Graduated to PumpSwap: {counts['graduated']} / {n} "
                 f"= {100.0*counts['graduated']/n:.1f}%** (population-level rate "
                 f"vs all minted tokens is ~2%; this is conditional on the sample filter)\n")
    lines.append("### Maximum multiple reached (across the observation window)")
    lines.append(f"- 2x or more: {counts['2x_or_more_max']} / {n} = {100.0*counts['2x_or_more_max']/n:.1f}%")
    lines.append(f"- **10x or more: {counts['10x_or_more_max']} / {n} = {100.0*counts['10x_or_more_max']/n:.1f}%**")
    lines.append(f"- **100x or more: {counts['100x_or_more_max']} / {n} = {100.0*counts['100x_or_more_max']/n:.1f}%**")
    lines.append(f"- **1000x or more: {counts['1000x_or_more_max']} / {n} = {100.0*counts['1000x_or_more_max']/n:.1f}%**\n")
    lines.append("### Final multiple (still up at day 60)")
    lines.append(f"- 10x or more: {counts['10x_or_more_final']} / {n} = {100.0*counts['10x_or_more_final']/n:.1f}%")
    lines.append(f"- 100x or more: {counts['100x_or_more_final']} / {n} = {100.0*counts['100x_or_more_final']/n:.1f}%")
    lines.append(f"- 1000x or more: {counts['1000x_or_more_final']} / {n} = {100.0*counts['1000x_or_more_final']/n:.1f}%\n")
    lines.append("### Outcome buckets")
    lines.append(f"- Rugged within 7d (dropped >95%): {counts['rugged_in_7d']} / {n} = {100.0*counts['rugged_in_7d']/n:.1f}%")
    lines.append(f"- Survived 60d (final ≥10% of entry): {counts['survived_60d']} / {n} = {100.0*counts['survived_60d']/n:.1f}%")
    lines.append(f"- Had a bounce (min hit BEFORE max, recovery ≥2x): {counts['had_bounce']} / {n} = {100.0*counts['had_bounce']/n:.1f}%")
    lines.append(f"- Multi-peak (≥2 local peaks within 80% of global): {counts['multi_peak']} / {n} = {100.0*counts['multi_peak']/n:.1f}%\n")

    lines.append("## Multiple distribution percentiles\n")
    mm = agg['max_multiple']
    fm = agg['final_multiple']
    lines.append(f"| Percentile | Max multiple | Final multiple |")
    lines.append(f"|---|---|---|")
    for p in ('p10', 'p25', 'p50', 'p75', 'p90', 'p99'):
        lines.append(f"| {p} | {mm[p]:.3f}x | {fm[p]:.3f}x |")
    lines.append(f"| mean | {mm['mean']:.3f}x | {fm['mean']:.3f}x |")
    lines.append(f"| max  | {mm['max']:.3f}x | {fm['max']:.3f}x |\n")

    lines.append("## Histogram: max multiple bucket counts\n")
    lines.append(f"| Bucket | Count | % of N |")
    lines.append(f"|---|---|---|")
    for label, count in agg['max_multiple_histogram'].items():
        lines.append(f"| {label} | {count} | {100.0*count/n:.1f}% |")
    lines.append("")

    lines.append("## Histogram: final multiple bucket counts\n")
    lines.append(f"| Bucket | Count | % of N |")
    lines.append(f"|---|---|---|")
    for label, count in agg['final_multiple_histogram'].items():
        lines.append(f"| {label} | {count} | {100.0*count/n:.1f}% |")
    lines.append("")

    lines.append("## Time-to-peak\n")
    dtp = agg['days_to_peak']
    lines.append(f"- Median days to peak: {dtp['p50']:.2f}")
    lines.append(f"- p10 / p90: {dtp['p10']:.2f} / {dtp['p90']:.2f}")
    lines.append(f"- Mean: {dtp['mean']:.2f}\n")

    surv = report.get('survivor_vs_rug', {})
    if surv:
        lines.append("## Survivor vs rug comparison\n")
        for tier, data in surv.items():
            if data.get('n', 0) == 0:
                lines.append(f"### {tier}: 0 tokens (skipped)")
                continue
            lines.append(f"### {tier} (N={data['n']})")
            lines.append(f"- median max multiple: {data['median_max_multiple']:.3f}x")
            lines.append(f"- median final multiple: {data['median_final_multiple']:.3f}x")
            lines.append(f"- median days to peak: {data['median_days_to_peak']:.2f}")
            lines.append(f"- % with bounce: {data['pct_with_bounce']:.1f}%")
            lines.append(f"- % multi-peak: {data['pct_multi_peak']:.1f}%\n")

    clust = report.get('clusters', {})
    if clust.get('available'):
        lines.append(f"## Path-shape clusters (k-means, k={clust['k']})\n")
        lines.append("Centroids show the log(price/entry) at each of 30 evenly-spaced "
                     "time samples across the 60-day window. peak_idx tells when the "
                     "shape's max occurs (0=mint, 29=day 60).\n")
        for cid, info in sorted(clust['clusters'].items(), key=lambda kv: -kv[1]['size']):
            lines.append(f"### Cluster {cid} — N={info['size']}, "
                         f"median final = {info['median_final_mult']:.3f}x")
            peak_pct_through = info['centroid_peak_idx'] / 29.0 * 100.0
            lines.append(f"- peak at sample {info['centroid_peak_idx']} "
                         f"(~{peak_pct_through:.0f}% through the window) — "
                         f"centroid peak log-mult = {info['centroid_peak_log_mult']:+.2f} "
                         f"(={math.exp(info['centroid_peak_log_mult']):.2f}x)")
            lines.append(f"- centroid final log-mult = {info['centroid_final_log_mult']:+.2f} "
                         f"(={math.exp(info['centroid_final_log_mult']):.2f}x)")
            sample_members = info['members'][:5]
            mem_strs = [f"{m['symbol'] or m['mint'][:8]}({m['max']:.1f}x→{m['final']:.2f}x)"
                        for m in sample_members]
            lines.append(f"- sample members: {', '.join(mem_strs)}\n")
    else:
        lines.append(f"## Path-shape clusters\n_Unavailable: {clust.get('reason', 'unknown')}_\n")

    return "\n".join(lines)


def write_csv(path: Path, features: list[TokenFeatures]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "mint", "symbol", "n_candles", "entry_price", "final_price",
            "max_multiple", "min_multiple", "final_multiple",
            "days_to_peak", "days_to_trough",
            "graduated", "rugged", "survived_60d", "had_bounce", "multi_peak",
        ])
        for ft in features:
            writer.writerow([
                ft.mint, ft.symbol or "", ft.n_candles,
                f"{ft.entry_price:.10g}", f"{ft.final_price:.10g}",
                f"{ft.max_multiple:.6g}", f"{ft.min_multiple:.6g}", f"{ft.final_multiple:.6g}",
                f"{ft.days_to_peak:.3f}", f"{ft.days_to_trough:.3f}",
                int(ft.graduated), int(ft.rugged), int(ft.survived_60d),
                int(ft.had_bounce), int(ft.multi_peak),
            ])


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    # Default window: pump.fun's earliest graduate era (Jan-Feb 2024).
    # Those tokens have ~2 YEARS of post-mint price history, giving the
    # cleanest 60-day lifecycle observation. The v3 API's ASC sort reaches
    # this era; DESC sort + graduated filter only reaches ~7-14 days back
    # from today which gives shorter observation windows.
    parser.add_argument("--from", dest="from_str", default="2024-01-25",
                        help="Mint window start (UTC date, inclusive)")
    parser.add_argument("--to", dest="to_str", default="2024-02-29",
                        help="Mint window end (UTC date, exclusive)")
    parser.add_argument("--sort", choices=("ASC", "DESC"), default="ASC",
                        help="ASC walks from earliest graduates forward in time "
                             "(best for old-token deep lifecycle research). "
                             "DESC walks from newest backward (best for recent "
                             "tokens, ~14d reachable).")
    parser.add_argument("--sample-size", type=int, default=20,
                        help="Target number of tokens to analyze")
    parser.add_argument("--clusters", type=int, default=5,
                        help="K for k-means path clustering")
    parser.add_argument("--out-dir", default="/tmp",
                        help="Directory to write report + CSV")
    parser.add_argument("--shuffle", action="store_true",
                        help="Shuffle discovered tokens before sampling (default: take first N)")
    parser.add_argument("--include-non-graduated", action="store_true",
                        help="Include non-graduated bonding-curve-only tokens. WARNING: "
                             "v3 offset cap is ~10k so without graduated filter the discovery "
                             "can only reach ~3-4 days back from today. Default OFF (graduated only).")
    args = parser.parse_args()

    try:
        from_dt = datetime.fromisoformat(args.from_str).replace(tzinfo=timezone.utc)
        to_dt = datetime.fromisoformat(args.to_str).replace(tzinfo=timezone.utc)
    except ValueError:
        print("ERROR: --from / --to must be ISO dates (YYYY-MM-DD)", file=sys.stderr)
        return 2
    if to_dt <= from_dt:
        print("ERROR: --to must be after --from", file=sys.stderr)
        return 2

    # Discover
    target_discovery = args.sample_size * 3  # over-discover so we can survive Birdeye misses
    tokens = discover_tokens(
        from_dt, to_dt, target_discovery,
        graduated_only=not args.include_non_graduated,
        sort_order=args.sort,
    )
    if not tokens:
        print("ERROR: no tokens discovered. Check pump.fun API + Birdeye API key.", file=sys.stderr)
        return 1

    if args.shuffle:
        random.seed(42)
        random.shuffle(tokens)
    sample = tokens[:args.sample_size]
    sources_used = sorted(set(t.discovery_source for t in sample))
    print(f"[sample] {len(sample)} tokens (from {len(tokens)} discovered, "
          f"sources: {sources_used})", file=sys.stderr)

    # Fetch + featurize
    features: list[TokenFeatures] = []
    excluded = 0
    n_with_history = 0
    for i, tok in enumerate(sample, 1):
        candles = fetch_history_birdeye(
            tok.mint, from_dt=tok.created_at or from_dt, days=HISTORY_DAYS,
        )
        time.sleep(BIRDEYE_SLEEP_SECONDS)
        if not candles:
            excluded += 1
            print(f"  [{i:>3}/{len(sample)}] {tok.symbol or tok.mint[:8]}: no candles",
                  file=sys.stderr)
            continue
        n_with_history += 1
        feats = compute_features(tok.mint, tok.symbol, candles, graduated=tok.graduated)
        if feats is None:
            excluded += 1
            continue
        features.append(feats)
        if i % 10 == 0 or i == len(sample):
            print(f"  [{i:>3}/{len(sample)}] {tok.symbol or tok.mint[:8]}: "
                  f"max={feats.max_multiple:.2f}x, final={feats.final_multiple:.3f}x",
                  file=sys.stderr)

    # Aggregate + cluster
    agg = aggregate(features)
    clusters = cluster_paths(features, k=args.clusters) if features else {"available": False, "reason": "no features"}
    survivor_rug = compare_survivor_vs_rug(features) if features else {}

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "from_dt": from_dt.date().isoformat(),
        "to_dt": to_dt.date().isoformat(),
        "n_discovered": len(tokens),
        "n_with_history": n_with_history,
        "n_analyzed": len(features),
        "n_excluded": excluded,
        "discovery_sources": sources_used,
        "aggregate": agg,
        "clusters": clusters,
        "survivor_vs_rug": survivor_rug,
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    md_path = out_dir / f"pumpfun_lifecycle_{stamp}.md"
    csv_path = out_dir / f"pumpfun_lifecycle_{stamp}.csv"
    md = render_markdown(report)
    md_path.write_text(md, encoding="utf-8")
    if features:
        write_csv(csv_path, features)

    print(f"\n[done] report: {md_path}", file=sys.stderr)
    print(f"[done] csv:    {csv_path}", file=sys.stderr)
    print(f"\n--- REPORT PREVIEW ---\n", file=sys.stderr)
    print(md, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
