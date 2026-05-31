"""Diagnostic probe: which pump.fun token-discovery endpoint actually works?

The lifecycle script's discovery returned 0 tokens on first run. Before
patching blind, run this to see which API endpoint actually responds with
data. Each probe prints HTTP status + first ~400 chars of body.

Usage:
  docker compose exec bot_copy python -m scripts.pumpfun_discovery_probe

No DB writes, no Birdeye CU consumption beyond a single test call.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


def _probe(label: str, url: str, headers: dict | None = None,
           method: str = "GET", body: dict | None = None) -> None:
    print(f"\n--- {label} ---", file=sys.stderr)
    print(f"  URL: {url}", file=sys.stderr)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers = {**(headers or {}), "Content-Type": "application/json"}
    headers = {
        "User-Agent": USER_AGENT_BROWSER,
        "Accept": "application/json, text/plain, */*",
        **(headers or {}),
    }
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15) as r:
            status = r.status
            body_bytes = r.read()
            body_text = body_bytes[:600].decode("utf-8", errors="replace")
            print(f"  Status: {status}", file=sys.stderr)
            print(f"  Body (first 600 bytes): {body_text}", file=sys.stderr)
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read()[:400].decode("utf-8", errors="replace")
        except Exception:
            pass
        print(f"  HTTPError: {e.code} {e.reason}", file=sys.stderr)
        print(f"  Error body: {err_body}", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"  URLError: {e.reason}", file=sys.stderr)
    except Exception as e:
        print(f"  {type(e).__name__}: {e}", file=sys.stderr)


def main() -> int:
    print("Probing pump.fun + Solana token discovery endpoints...", file=sys.stderr)

    # ----- pump.fun v3 with various TIME FILTER params (the load-bearing question) -----
    # The default sort returns newest-first. To reach a historical window we
    # either need: (a) an API time filter, or (b) deep pagination. Probe (a).
    target_ms_start = 1740787200000   # 2025-03-01
    target_ms_end = 1742083200000     # 2025-03-16
    _probe(
        "v3 + offset (deep pagination test, page 200)",
        "https://frontend-api-v3.pump.fun/coins?offset=10000&limit=5",
    )
    _probe(
        "v3 + sort=created_timestamp&order=ASC",
        "https://frontend-api-v3.pump.fun/coins?offset=0&limit=5&sort=created_timestamp&order=ASC",
    )
    _probe(
        "v3 + before_timestamp (target window param trial)",
        f"https://frontend-api-v3.pump.fun/coins?offset=0&limit=5&before_timestamp={target_ms_end}",
    )
    _probe(
        "v3 + start_time / end_time (alternative names)",
        f"https://frontend-api-v3.pump.fun/coins?offset=0&limit=5&start_time={target_ms_start}&end_time={target_ms_end}",
    )
    _probe(
        "v3 + created_after / created_before",
        f"https://frontend-api-v3.pump.fun/coins?offset=0&limit=5&created_after={target_ms_start}&created_before={target_ms_end}",
    )
    _probe(
        "v3 + complete=true (only graduated)",
        "https://frontend-api-v3.pump.fun/coins?offset=0&limit=5&complete=true",
    )

    # ----- pump.fun frontend API (current) -----
    _probe(
        "pumpfun frontend-api /coins (no params)",
        "https://frontend-api.pump.fun/coins?offset=0&limit=5",
    )
    _probe(
        "pumpfun frontend-api /coins (sort created)",
        "https://frontend-api.pump.fun/coins?offset=0&limit=5"
        "&sort=created_timestamp&order=ASC&includeNsfw=false",
    )

    # ----- pump.fun newer v3 API -----
    _probe(
        "pumpfun frontend-api v3 (alternative path)",
        "https://frontend-api-v3.pump.fun/coins?offset=0&limit=5",
    )
    _probe(
        "pumpfun api.pump.fun /coins",
        "https://api.pump.fun/v1/coins?limit=5",
    )
    _probe(
        "pumpfun swap-api /coins",
        "https://swap-api.pump.fun/coins?offset=0&limit=5",
    )

    # ----- DexScreener (public, no auth) -----
    _probe(
        "dexscreener search pump.fun",
        "https://api.dexscreener.com/latest/dex/search?q=pump",
    )
    _probe(
        "dexscreener token-profiles latest",
        "https://api.dexscreener.com/token-profiles/latest/v1",
    )

    # ----- Birdeye (requires API key) -----
    api_key = os.environ.get("BIRDEYE_API_KEY")
    if not api_key:
        print("\n[birdeye] BIRDEYE_API_KEY not in env — skipping Birdeye probes", file=sys.stderr)
    else:
        birdeye_headers = {
            "X-API-KEY": api_key, "x-chain": "solana",
            "Accept": "application/json",
        }
        _probe(
            "birdeye /defi/tokenlist (sort by created)",
            "https://public-api.birdeye.so/defi/tokenlist?sort_by=v24hUSD&sort_type=desc&offset=0&limit=5",
            headers=birdeye_headers,
        )
        _probe(
            "birdeye /defi/v3/token/list_v3 (NEW: source=pumpfun)",
            "https://public-api.birdeye.so/defi/v3/token/list_v3?offset=0&limit=5&source=pumpfun",
            headers=birdeye_headers,
        )
        _probe(
            "birdeye /defi/v3/launchpad/list (alternative)",
            "https://public-api.birdeye.so/defi/v3/launchpad/list?launchpad=pumpfun&limit=5",
            headers=birdeye_headers,
        )

    # ----- Helius DAS searchAssets (uses HELIUS_API_KEY) -----
    helius_key = os.environ.get("HELIUS_API_KEY")
    if not helius_key:
        print("\n[helius] HELIUS_API_KEY not in env — skipping Helius probes", file=sys.stderr)
    else:
        helius_url = f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
        # searchAssets: filter by tokenType=fungible, sorted by created (descending)
        _probe(
            "helius DAS searchAssets (fungible tokens, desc created)",
            helius_url,
            method="POST",
            body={
                "jsonrpc": "2.0", "id": "probe-1",
                "method": "searchAssets",
                "params": {
                    "tokenType": "fungible",
                    "page": 1, "limit": 5,
                    "sortBy": {"sortBy": "created", "sortDirection": "desc"},
                },
            },
        )

    print("\n--- DONE ---", file=sys.stderr)
    print("Look for endpoints returning Status: 200 with JSON content.", file=sys.stderr)
    print("I'll patch pumpfun_lifecycle_analysis.py to use the one that works.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
