"""API-based wallet vetter (2026-11-17) — replaces the headless Birdeye page scraper.

Birdeye put a Cloudflare challenge in front of its wallet pages (Sept 2026), so the headless
Playwright vetter returned "no metrics rendered" for every wallet and wallet discovery silently
stopped. This vetter uses our PAID Birdeye API instead (no scraping):
  /wallet/v2/pnl/summary         realized / unrealized / trades / unique tokens   (~2 credits)
  /v1/wallet/tx_list             recent transactions (≤100 per page)              (~50 credits/page)
  /wallet/v2/pnl?token_addresses per-token realized P&L + buy/sell counts          (cheap)

Rules follow the RECALIBRATED prompts (2026-11-17, scored against our own P&L):
  cluster    KEEP needs realized ≥ $5k, not an MM bot, ≥2 multi-x wins in the sample, and MOST
             OF ITS PROFIT from holds ≥ 15 min (profit-weighted, NOT median hold). A bag-holder
             with strong entries is UNCERTAIN, not REJECT.
  conviction KEEP needs realized ≥ $5k on ≥ 10 tokens, accumulation (median buys/token ≥ 3),
             mid-length holds (5m–24h ≥ 30% of tokens), and that it EXITS (sells ≥ 80% of what it
             bought on ≥ 60% of tokens). The 2x rate is reported, never a gate. BAGHOLDER only if
             it rarely exits.
Bot-farm (BUNDLE) grouping is done across a batch by the caller (see bundle_groups).

Pure parsing/classification functions are unit-tested; fetch_* do the network I/O.
Known limitation: no token-liquidity-at-trade-time check (the thin-mint rule) — Birdeye's API
doesn't expose historical depth; new KEEPs enter on PROBATION, so our own results catch that.
"""
from __future__ import annotations

import json
import statistics
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from typing import Optional

BASE = "https://public-api.birdeye.so"
QUOTE_MINTS = ("So11111111111111111111111111111111111111112",
               "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
               "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB")   # USDT
CALL_GAP_S = 1.2


class Birdeye:
    def __init__(self, api_key: str):
        self.key = api_key
        self.calls = 0

    def get(self, path: str, tries: int = 5) -> Optional[dict]:
        for i in range(tries):
            req = urllib.request.Request(BASE + path, headers={
                "X-API-KEY": self.key, "x-chain": "solana", "accept": "application/json",
                "User-Agent": "Mozilla/5.0"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    out = json.loads(r.read())
                self.calls += 1
                time.sleep(CALL_GAP_S)
                return out
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(3 * (i + 1))
                    continue
                return None
            except Exception:
                time.sleep(2)
        return None


# ---------------------------------------------------------------- parsing (pure) ----
def parse_swaps(txs: list[dict]) -> list[tuple[str, str, float]]:
    """(token, 'buy'|'sell', unix_ts) per tx where exactly one non-quote token changed."""
    out = []
    for t in txs:
        changes = [b for b in (t.get("balanceChange") or [])
                   if b.get("address") and b.get("address") not in QUOTE_MINTS and b.get("amount")]
        if len(changes) != 1:
            continue
        try:
            ts = datetime.fromisoformat(t["blockTime"]).timestamp()
        except Exception:
            continue
        b = changes[0]
        out.append((b["address"], "buy" if b["amount"] > 0 else "sell", ts))
    return out


def hold_spans(swaps: list[tuple[str, str, float]]) -> dict[str, float]:
    """token -> minutes from first buy to last sell, for tokens with both inside the window."""
    first_buy, last_sell = {}, {}
    for tok, side, ts in swaps:
        if side == "buy":
            first_buy[tok] = min(ts, first_buy.get(tok, ts))
        else:
            last_sell[tok] = max(ts, last_sell.get(tok, ts))
    return {t: (last_sell[t] - first_buy[t]) / 60 for t in first_buy
            if t in last_sell and last_sell[t] >= first_buy[t]}


def hold_bins(spans: dict[str, float]) -> dict[str, float]:
    n = len(spans) or 1
    v = list(spans.values())
    return {"lt1m": sum(x < 1 for x in v) / n, "mid_5m_24h": sum(5 <= x < 1440 for x in v) / n,
            "gt7d": sum(x >= 10080 for x in v) / n, "n": len(spans)}


def profit_share_ge15m(spans: dict[str, float], token_pnl: dict[str, dict]) -> Optional[float]:
    """Share of POSITIVE realized profit (sampled tokens with a measured hold) from holds >= 15m."""
    win = {t: (token_pnl.get(t) or {}).get("realized", 0.0) for t in spans}
    pos = {t: p for t, p in win.items() if p > 0}
    total = sum(pos.values())
    if total <= 0:
        return None
    return sum(p for t, p in pos.items() if spans[t] >= 15) / total


def classify(summary: dict, spans: dict[str, float], token_pnl: dict[str, dict]) -> dict:
    """Return {'cluster': verdict, 'conviction': verdict, 'metrics': {...}, 'reason': str}."""
    s = (summary or {}).get("summary") or {}
    pnl, counts = s.get("pnl") or {}, s.get("counts") or {}
    realized = float(pnl.get("realized_profit_usd") or 0)
    unrealized = float(pnl.get("unrealized_usd") or 0)
    n_tok = int(s.get("unique_tokens") or 0)
    total_trade = int(counts.get("total_trade") or 0)
    avg_per_trade = float(pnl.get("avg_profit_per_trade_usd") or 0)
    bins = hold_bins(spans)
    share15 = profit_share_ge15m(spans, token_pnl)
    sampled = [v for v in token_pnl.values()]
    multi_x = sum(1 for v in sampled if (v.get("realized_pct") or 0) >= 100)
    pct_2x = multi_x / len(sampled) if sampled else 0.0
    med_buys = statistics.median([v.get("buys", 0) for v in sampled]) if sampled else 0
    exits = [v for v in sampled if v.get("bought_qty")]
    exit_share = (sum(1 for v in exits if (v.get("sold_qty") or 0) >= 0.8 * v["bought_qty"]) / len(exits)
                  if exits else 0.0)
    bag = unrealized < 0 and -unrealized > realized
    mm = total_trade > 1_000_000 or (total_trade > 100_000 and avg_per_trade < 1.0)
    m = {"realized": round(realized), "unrealized": round(unrealized), "n_tok": n_tok,
         "total_trade": total_trade, "sample_tokens": len(sampled), "held_tokens": bins["n"],
         "lt1m": round(bins["lt1m"], 2), "mid": round(bins["mid_5m_24h"], 2), "gt7d": round(bins["gt7d"], 2),
         "profit_share_ge15m": None if share15 is None else round(share15, 2),
         "multi_x": multi_x, "pct_2x": round(pct_2x, 3), "med_buys": med_buys, "exit_share": round(exit_share, 2)}

    # cluster
    if mm:
        cl, why = "MM_BOT", "market-maker pattern"
    elif realized <= 0:
        cl, why = "REJECT", "realized <= 0"
    elif realized < 5000:
        cl, why = "LOW_SAMPLE", "realized < $5k"
    elif share15 is None or bins["n"] < 5:
        cl, why = "UNCERTAIN", f"too few measured holds ({bins['n']})"
    elif share15 < 0.5:
        cl, why = "TOO_FAST", f"only {share15:.0%} of profit from >=15m holds"
    elif multi_x < 2:
        cl, why = "REJECT", f"multi-x wins {multi_x} < 2 in sample"
    elif bag:
        cl, why = "UNCERTAIN", "bag exceeds realized but entries strong"
    else:
        cl, why = "KEEP", f"{share15:.0%} profit >=15m, {multi_x} multi-x"

    # conviction
    if realized <= 0:
        cv = "BAGHOLDER" if bag else "REJECT"
    elif realized < 5000 or n_tok < 10:
        cv = "LOW_SAMPLE"
    elif bins["n"] >= 5 and bins["lt1m"] > 0.6:
        cv = "TOO_FAST_FOR_CONVICTION"
    elif bins["n"] >= 5 and bins["gt7d"] > 0.6:
        cv = "TOO_SLOW"
    elif bag and exit_share < 0.6:
        cv = "BAGHOLDER"
    elif med_buys < 3:
        cv = "SNIPER" if med_buys <= 1 else "REJECT"
    elif bins["n"] < 5:
        cv = "UNCERTAIN"
    elif bins["mid_5m_24h"] >= 0.3 and exit_share >= 0.6:
        cv = "KEEP_CONVICTION"
    else:
        cv = "REJECT"
    return {"cluster": cl, "conviction": cv, "metrics": m, "reason": why}


def bundle_groups(swaps_by_wallet: dict[str, list[tuple[str, str, float]]],
                  min_shared: int = 10, min_pct: float = 0.9, window_s: float = 60) -> list[set]:
    """Groups of wallets (within one batch) whose buys of shared tokens land within window_s on
    >= min_pct of each wallet's bought tokens. One group = one operator."""
    first = {w: {} for w in swaps_by_wallet}
    for w, sw in swaps_by_wallet.items():
        for tok, side, ts in sw:
            if side == "buy":
                first[w][tok] = min(ts, first[w].get(tok, ts))
    ws = list(first)
    parent = {w: w for w in ws}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(ws):
        for b in ws[i + 1:]:
            shared = [t for t in first[a] if t in first[b] and abs(first[a][t] - first[b][t]) <= window_s]
            if (len(shared) >= min_shared and len(shared) >= min_pct * len(first[a])
                    and len(shared) >= min_pct * len(first[b])):
                parent[find(a)] = find(b)
    groups = defaultdict(set)
    for w in ws:
        groups[find(w)].add(w)
    return [g for g in groups.values() if len(g) > 1]


# ---------------------------------------------------------------- fetching ----
def fetch_wallet(be: Birdeye, wallet: str, max_pages: int = 5) -> Optional[dict]:
    """summary + recent swaps + per-token P&L for the swapped tokens. None if the summary fails."""
    summ = be.get(f"/wallet/v2/pnl/summary?wallet={wallet}")
    if not summ or not summ.get("data"):
        return None
    txs, before = [], None
    for _ in range(max_pages):
        q = f"/v1/wallet/tx_list?wallet={wallet}&limit=100" + (f"&before={before}" if before else "")
        page = ((be.get(q) or {}).get("data") or {}).get("solana") or []
        if not page:
            break
        txs += page
        before = page[-1].get("txHash")
        if len(page) < 100:
            break
    swaps = parse_swaps(txs)
    tokens = list(dict.fromkeys(t for t, _, _ in swaps))[:100]
    token_pnl = {}
    for i in range(0, len(tokens), 20):
        d = be.get(f"/wallet/v2/pnl?wallet={wallet}&token_addresses={','.join(tokens[i:i + 20])}")
        for addr, v in (((d or {}).get("data") or {}).get("tokens") or {}).items():
            p, c, q = v.get("pnl") or {}, v.get("counts") or {}, v.get("quantity") or {}
            token_pnl[addr] = {"realized": float(p.get("realized_profit_usd") or 0),
                               "realized_pct": float(p.get("realized_profit_percent") or 0),
                               "buys": int(c.get("total_buy") or 0),
                               "bought_qty": float(q.get("total_bought_amount") or 0),
                               "sold_qty": float(q.get("total_sold_amount") or 0)}
    return {"summary": summ["data"], "swaps": swaps, "token_pnl": token_pnl, "n_tx": len(txs)}


def vet_wallet(be: Birdeye, wallet: str) -> dict:
    d = fetch_wallet(be, wallet)
    if d is None:
        return {"address": wallet, "cluster": "ERROR", "conviction": "ERROR", "reason": "summary fetch failed",
                "metrics": {}, "swaps": []}
    out = classify(d["summary"], hold_spans(d["swaps"]), d["token_pnl"])
    out.update({"address": wallet, "swaps": d["swaps"]})
    out["metrics"]["n_tx"] = d["n_tx"]
    return out
