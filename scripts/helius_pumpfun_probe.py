"""Cheap probe before committing to full Helius scan.

Fetches a handful of recent transactions from pump.fun's bonding-curve
program via Helius's parsed-transaction endpoint. Reports:

1. Helius's `type` classification distribution — tells us whether
   graduations are a distinct queryable type (cheap filter) or if we
   have to fetch + parse all txs client-side (expensive).
2. Graduation density in the sample — extrapolates feasibility of a
   12-month full scan.
3. Raw structure of one transaction with the "Complete" / migration
   instruction so we know how to detect graduations.

Costs ~200 Helius credits total. Output drives the decision on full
scan vs sample-based estimation.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone


# Pump.fun bonding curve program (Solana mainnet)
PUMPFUN_BONDING_CURVE_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
# PumpSwap (the AMM tokens migrate to on graduation) — alternative target
PUMPSWAP_PROGRAM_CANDIDATES = [
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",   # PumpSwap AMM (post-March 2025)
    "PSwapMdSai8tjrEXcxFeQth87xC4rRsa4VA5mhGhXkP",   # alternate candidate
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


def fetch_address_transactions(address: str, api_key: str, limit: int = 100,
                                before: str | None = None) -> list[dict]:
    """Helius enhanced transactions for an address.

    Returns parsed/decoded txs (~1 credit per tx in the response).
    """
    params = {"api-key": api_key, "limit": str(limit)}
    if before:
        params["before"] = before
    url = f"https://api.helius.xyz/v0/addresses/{address}/transactions?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status != 200:
                print(f"  HTTP {r.status}", file=sys.stderr)
                return []
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = e.read()[:500].decode("utf-8", errors="replace")
        except Exception:
            body = ""
        print(f"  HTTPError {e.code}: {body}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"  {type(e).__name__}: {e}", file=sys.stderr)
        return []


def analyze_batch(txs: list[dict], label: str) -> None:
    if not txs:
        print(f"\n=== {label}: 0 txs returned ===", file=sys.stderr)
        return
    type_counter: Counter[str] = Counter()
    source_counter: Counter[str] = Counter()
    description_samples: list[str] = []
    grad_candidates: list[dict] = []
    earliest_ts = None
    latest_ts = None

    for tx in txs:
        type_counter[tx.get("type") or "UNKNOWN"] += 1
        source_counter[tx.get("source") or "UNKNOWN"] += 1
        desc = tx.get("description") or ""
        if desc and len(description_samples) < 5:
            description_samples.append(desc[:200])
        ts = tx.get("timestamp", 0)
        if ts:
            if earliest_ts is None or ts < earliest_ts:
                earliest_ts = ts
            if latest_ts is None or ts > latest_ts:
                latest_ts = ts
        # Heuristic graduation detection: description mentions "complete" / "graduat" / "migrate"
        desc_lower = desc.lower()
        if "complete" in desc_lower or "graduat" in desc_lower or "migrat" in desc_lower:
            grad_candidates.append(tx)
        # Or check instructions for keywords
        for instr in (tx.get("instructions") or []):
            iname = (instr.get("name") or "").lower()
            if "complete" in iname or "graduat" in iname or "migrat" in iname:
                if tx not in grad_candidates:
                    grad_candidates.append(tx)
                break

    print(f"\n=== {label}: {len(txs)} txs ===", file=sys.stderr)
    if earliest_ts and latest_ts:
        e = datetime.fromtimestamp(earliest_ts, tz=timezone.utc).isoformat()
        l = datetime.fromtimestamp(latest_ts, tz=timezone.utc).isoformat()
        span_seconds = latest_ts - earliest_ts
        print(f"  Time span: {e} → {l} ({span_seconds}s = {span_seconds/3600:.1f}h)",
              file=sys.stderr)
        if span_seconds > 0:
            txs_per_day = (len(txs) / span_seconds) * 86400
            print(f"  -> {txs_per_day:.0f} txs/day extrapolated", file=sys.stderr)

    print(f"\n  Helius `type` distribution:", file=sys.stderr)
    for t, n in type_counter.most_common():
        print(f"    {t}: {n}", file=sys.stderr)
    print(f"\n  Helius `source` distribution:", file=sys.stderr)
    for s, n in source_counter.most_common():
        print(f"    {s}: {n}", file=sys.stderr)

    print(f"\n  Sample descriptions (first 5):", file=sys.stderr)
    for d in description_samples:
        print(f"    > {d}", file=sys.stderr)

    print(f"\n  Heuristic graduation candidates (complete/migrate/graduat keyword): "
          f"{len(grad_candidates)} of {len(txs)} = {100*len(grad_candidates)/len(txs):.1f}%",
          file=sys.stderr)
    if grad_candidates:
        print(f"\n  First graduation candidate (full payload, truncated):",
              file=sys.stderr)
        sample = json.dumps(grad_candidates[0], indent=2)[:3000]
        print(f"    {sample}", file=sys.stderr)


def main() -> int:
    api_key = os.environ.get("HELIUS_API_KEY")
    if not api_key:
        print("ERROR: HELIUS_API_KEY not in env", file=sys.stderr)
        return 1

    # Probe 1: pump.fun bonding curve program
    print(f"\n##############################", file=sys.stderr)
    print(f"# PROBE 1: pump.fun bonding curve", file=sys.stderr)
    print(f"##############################", file=sys.stderr)
    txs = fetch_address_transactions(PUMPFUN_BONDING_CURVE_PROGRAM, api_key, limit=100)
    analyze_batch(txs, f"pump.fun bonding curve ({PUMPFUN_BONDING_CURVE_PROGRAM})")

    # Probe 2-3: PumpSwap AMM candidates
    for prog in PUMPSWAP_PROGRAM_CANDIDATES:
        print(f"\n##############################", file=sys.stderr)
        print(f"# PROBE: PumpSwap candidate {prog}", file=sys.stderr)
        print(f"##############################", file=sys.stderr)
        txs = fetch_address_transactions(prog, api_key, limit=100)
        analyze_batch(txs, f"PumpSwap candidate ({prog})")

    print(f"\n##############################", file=sys.stderr)
    print(f"# Interpretation guide", file=sys.stderr)
    print(f"##############################", file=sys.stderr)
    print(f"  - If `type` distribution has a clear 'TOKEN_MIGRATION' or graduation-", file=sys.stderr)
    print(f"    specific type → cheap filtered scan is feasible (~50k credits).", file=sys.stderr)
    print(f"  - If keyword-heuristic graduation rate is >10% → density is high enough", file=sys.stderr)
    print(f"    that a sampled scan can extract graduations cheaply.", file=sys.stderr)
    print(f"  - If extrapolated txs/day > 1M and no type filter exists → full scan", file=sys.stderr)
    print(f"    is infeasible; we need sample-based estimation or a different angle.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
