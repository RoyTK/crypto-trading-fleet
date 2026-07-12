"""Phase 1 collector (non-Telegram arm): retro first-mention/promo timestamps per runner.

For each token in the exported runner corpus (scratchpad/runner_corpus.json, from
prerun_scans), collects:
  - Dexscreener paid-promo orders  (orders/v1/solana/{token} -> paymentTimestamp ms)
  - pump.fun coin detail           (created_timestamp, king_of_the_hill_timestamp,
                                    complete/graduation)
  - warosu /biz/ archive mentions  (search by CA; earliest + count, best-effort parse)

Output: one JSON with raw timestamps per token per source. The lead/lag JOIN
(vs intraday run_start + earliest accumulator buy) happens in the analysis step.
Telegram (Telethon) runs as a separate collector once the session exists.

Usage (local Windows or anywhere with internet):
  python scripts/research_source_leadlag.py --corpus <runner_corpus.json> --out <hits.json>
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/126.0.0.0 Safari/537.36")}

DEX_ORDERS = "https://api.dexscreener.com/orders/v1/solana/{token}"
PUMP_COIN = "https://frontend-api-v3.pump.fun/coins/{token}"
WAROSU = "https://warosu.org/biz/?task=search2&search_text={token}"

# warosu post timestamps appear as unix-seconds in span title attrs / data fields;
# grab any 10-digit epoch in a plausible range (2020..2030).
EPOCH_RE = re.compile(r"\b(1[6-9]\d{8})\b")


def get(url: str, timeout: int = 25) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def try_get(url: str, retries: int = 3, backoff: float = 5.0):
    for i in range(retries):
        try:
            return get(url)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(backoff * (i + 1))
                continue
            return e.code, b""
        except Exception:
            time.sleep(2)
    return 0, b""


def collect_dex_orders(token: str) -> dict:
    status, body = try_get(DEX_ORDERS.format(token=token))
    if status != 200:
        return {"status": status}
    try:
        d = json.loads(body)
    except Exception:
        return {"status": "parse_error"}
    # response: {"orders":[{type,status,paymentTimestamp},...], "boosts":[...]}
    orders = d.get("orders") or (d if isinstance(d, list) else [])
    boosts = d.get("boosts") or []
    return {
        "status": 200,
        "orders": [
            {"type": o.get("type"), "order_status": o.get("status"),
             "payment_ts_ms": o.get("paymentTimestamp")}
            for o in orders
        ],
        "n_boosts": len(boosts),
        "boosts": boosts[:5],
    }


def collect_pumpfun(token: str) -> dict:
    status, body = try_get(PUMP_COIN.format(token=token))
    if status != 200:
        return {"status": status}
    try:
        d = json.loads(body)
    except Exception:
        return {"status": "parse_error"}
    return {
        "status": 200,
        "created_ts_ms": d.get("created_timestamp"),
        "koth_ts_ms": d.get("king_of_the_hill_timestamp"),
        "graduated": d.get("complete"),
        "creator": d.get("creator"),
        "has_twitter": bool(d.get("twitter")),
        "has_telegram": bool(d.get("telegram")),
    }


def collect_warosu(token: str) -> dict:
    status, body = try_get(WAROSU.format(token=token))
    if status != 200:
        return {"status": status}
    html = body.decode("utf-8", errors="replace")
    if "No results found" in html or "returned no results" in html.lower():
        return {"status": 200, "hits": 0}
    epochs = sorted({int(m) for m in EPOCH_RE.findall(html)})
    n_posts = html.count('class="comment')
    return {
        "status": 200,
        "hits": n_posts or len(epochs),
        "earliest_epoch_s": epochs[0] if epochs else None,
        "latest_epoch_s": epochs[-1] if epochs else None,
        "note": "page-1 parse; ordering not guaranteed oldest-first",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0, help="0 = all")
    args = ap.parse_args()

    corpus = json.load(open(args.corpus))
    if args.limit:
        corpus = corpus[: args.limit]

    results = []
    t0 = time.time()
    for i, row in enumerate(corpus):
        token = row["token"]
        rec = {**row}
        rec["dexscreener"] = collect_dex_orders(token)
        time.sleep(1.1)  # 60 rpm limit
        rec["pumpfun"] = collect_pumpfun(token)
        time.sleep(1.0)
        rec["warosu"] = collect_warosu(token)
        time.sleep(2.5)  # be polite to the archive
        results.append(rec)
        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(corpus)} tokens ({time.time() - t0:.0f}s)")

    with open(args.out, "w") as f:
        json.dump(results, f, indent=1)

    # quick tallies
    n = len(results)
    dex_paid = sum(1 for r in results
                   if r["dexscreener"].get("orders"))
    pf = sum(1 for r in results if r["pumpfun"].get("status") == 200)
    wa = sum(1 for r in results if (r["warosu"].get("hits") or 0) > 0)
    print(f"\nDONE {n} tokens -> {args.out}")
    print(f"  dexscreener paid-promo present: {dex_paid}/{n}")
    print(f"  pump.fun coin found:            {pf}/{n}")
    print(f"  warosu /biz/ mentioned:         {wa}/{n}")
    return 0


if __name__ == "__main__":
    sys_exit = main()
    raise SystemExit(sys_exit)
