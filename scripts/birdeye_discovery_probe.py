"""Probe Birdeye + DexScreener endpoints for last-12-months token discovery.

Goal: find a discovery source that returns Solana tokens (ideally pump.fun
graduates) with creation timestamps so we can filter the last 12 months.

Tests several endpoints with sample responses printed. Look at the output to
decide which one supports the discovery we need.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


def _probe(label: str, url: str, headers: dict | None = None,
           method: str = "GET", body: dict | None = None,
           show_full: bool = False) -> None:
    print(f"\n=== {label} ===", file=sys.stderr)
    print(f"  URL: {url}", file=sys.stderr)
    data_bytes = None
    if body is not None:
        data_bytes = json.dumps(body).encode("utf-8")
        headers = {**(headers or {}), "Content-Type": "application/json"}
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        **(headers or {}),
    }
    try:
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15) as r:
            status = r.status
            body_bytes = r.read()
            print(f"  Status: {status}", file=sys.stderr)
            try:
                parsed = json.loads(body_bytes.decode("utf-8", errors="replace"))
                # Print a sampled subset
                if show_full:
                    sample = json.dumps(parsed, indent=2)[:2500]
                else:
                    # Show structure + first item
                    sample = _summarize(parsed)
                print(f"  Body sample:\n{sample}", file=sys.stderr)
            except json.JSONDecodeError:
                print(f"  Non-JSON body (first 400b): {body_bytes[:400]!r}", file=sys.stderr)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read()[:300].decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        print(f"  HTTPError {e.code}: {err_body}", file=sys.stderr)
    except Exception as e:
        print(f"  {type(e).__name__}: {e}", file=sys.stderr)


def _summarize(parsed) -> str:
    """Show structure + first item's full payload (to inspect available fields)."""
    if isinstance(parsed, dict):
        out = ["{"]
        for k, v in parsed.items():
            if isinstance(v, dict):
                inner = ", ".join(list(v.keys())[:8])
                out.append(f"  {k!r}: dict({inner})")
                if "items" in v and isinstance(v["items"], list) and v["items"]:
                    out.append(f"    items[0] = {json.dumps(v['items'][0], indent=2)[:1500]}")
                elif "tokens" in v and isinstance(v["tokens"], list) and v["tokens"]:
                    out.append(f"    tokens[0] = {json.dumps(v['tokens'][0], indent=2)[:1500]}")
            elif isinstance(v, list):
                out.append(f"  {k!r}: list[{len(v)}]")
                if v:
                    out.append(f"    [0] = {json.dumps(v[0], indent=2)[:1200]}")
            else:
                out.append(f"  {k!r}: {v!r}")
        out.append("}")
        return "\n".join(out)
    elif isinstance(parsed, list):
        out = [f"list[{len(parsed)}]"]
        if parsed:
            out.append(f"  [0] = {json.dumps(parsed[0], indent=2)[:1500]}")
        return "\n".join(out)
    return repr(parsed)[:1500]


def main() -> int:
    api_key = os.environ.get("BIRDEYE_API_KEY")
    if not api_key:
        print("ERROR: BIRDEYE_API_KEY not in env", file=sys.stderr)
        return 1
    be_headers = {
        "X-API-KEY": api_key,
        "x-chain": "solana",
        "Accept": "application/json",
    }

    # ------------------ Birdeye tokenlist variants ------------------
    _probe(
        "birdeye /defi/tokenlist (top by 24h volume)",
        "https://public-api.birdeye.so/defi/tokenlist?sort_by=v24hUSD&sort_type=desc&offset=0&limit=2",
        headers=be_headers,
    )
    _probe(
        "birdeye /defi/tokenlist sort_by=created_at",
        "https://public-api.birdeye.so/defi/tokenlist?sort_by=created_at&sort_type=desc&offset=0&limit=2",
        headers=be_headers,
    )
    _probe(
        "birdeye /defi/tokenlist sort_by=mc",
        "https://public-api.birdeye.so/defi/tokenlist?sort_by=mc&sort_type=desc&offset=0&limit=2",
        headers=be_headers,
    )

    # ------------------ Birdeye v3 variants ------------------
    _probe(
        "birdeye /defi/v3/token/list",
        "https://public-api.birdeye.so/defi/v3/token/list?offset=0&limit=2&sort_by=v24hUSD&sort_type=desc",
        headers=be_headers,
    )
    _probe(
        "birdeye /defi/v3/token/list_scroll (cursor pagination)",
        "https://public-api.birdeye.so/defi/v3/token/list_scroll?limit=2",
        headers=be_headers,
    )
    _probe(
        "birdeye /defi/v3/token/trending",
        "https://public-api.birdeye.so/defi/v3/token/trending?offset=0&limit=2&sort_by=rank&sort_type=asc",
        headers=be_headers,
    )
    _probe(
        "birdeye /defi/v3/all_time_high",
        "https://public-api.birdeye.so/defi/v3/all_time_high?offset=0&limit=2",
        headers=be_headers,
    )
    _probe(
        "birdeye /defi/v3/token/new_listing",
        "https://public-api.birdeye.so/defi/v3/token/new_listing?offset=0&limit=2",
        headers=be_headers,
    )
    _probe(
        "birdeye /defi/trending_tokens",
        "https://public-api.birdeye.so/defi/trending_tokens?offset=0&limit=2",
        headers=be_headers,
    )

    # ------------------ DexScreener variants ------------------
    _probe(
        "dexscreener token-profiles latest",
        "https://api.dexscreener.com/token-profiles/latest/v1",
        headers={"User-Agent": USER_AGENT},
    )
    _probe(
        "dexscreener pairs/solana (no path)",
        "https://api.dexscreener.com/latest/dex/pairs/solana",
        headers={"User-Agent": USER_AGENT},
    )
    _probe(
        "dexscreener search PumpSwap",
        "https://api.dexscreener.com/latest/dex/search?q=pumpswap",
        headers={"User-Agent": USER_AGENT},
    )

    print("\n=== DONE ===", file=sys.stderr)
    print("Look for endpoints that return tokens WITH a creation/launch timestamp.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
