"""Headless wallet vetter — computes the cluster QUALIFICATION screen from Birdeye LITE raw
data (no browser, no Premium): /trader/txs/seek_by_time (raw trades) + /v1/wallet/token_list
(current holdings). Approximates Birdeye's precomputed wallet-analyzer metrics from RECENT
trade history (paged, capped MAX_PAGES) — directional, meant for the COARSE KEEP/REJECT/
TOO_FAST screen, not dollar-exact. Validated 2026-07-15 against browser-Opus's verdicts.

Default = COMPARE mode: prints my verdict + metrics alongside browser-Opus's verdicts and an
agreement summary. (Writing to vetted_watch_results.txt is intentionally not done here — the
attended run already wrote it; enable append later once the screen is trusted.)
"""
import time, json, urllib.request, urllib.error, statistics as st
from collections import defaultdict
from bots.copy.config import get_copy_settings

KEY = get_copy_settings().birdeye_api_key
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"
MAX_PAGES = 25       # ~2500 most-recent trades (paging cap = the approximation's main limit)
DUST = 25.0
SIGNIF_USD = 2000.0  # "realized significantly positive"
MULTIX_MIN = 3       # "multiple multi-x wins" (one-hit-wonder rejected)
TOO_FAST_MIN = 15.0  # median hold < 15 min = COPY can't follow

# (address, browser-Opus verdict) — the ground truth to validate against, in batch rank order.
BATCH = [
 ("BSHdFzWq6BfXpTx49LcCuvF4FVZakEZTibkKgjBcJqLD", "REJECT"),
 ("FLiYfD2z5NRpCuBg2hx6zM4UooVtvtD6ssQFUNpuPGEQ", "KEEP"),
 ("4YAXTiuaVhzUFDasQabjGxWPKZiFRKoLUpnAwEagH6dX", "REJECT"),
 ("CZdSjfAY6BNz1nphGPyVr5rdaA1c62kcDKnfMiL1Fpa2", "REJECT"),
 ("93kgxYKex5wPyEkjH9P4KspLFAi3fdWBkw8vZce51pbp", "TOO_FAST"),
 ("3fnSm6oDRSEBsb4vARAVdxdT12ZjsompKXd4XJCGqCSX", "REJECT"),
 ("Bernard5o8FFRVnjKux3MmSX53u8EMuSDGbhuK69Bpyw", "KEEP"),
 ("F7bGJRDR7yuQeXSagqS4MeMVA8Ykc6z61VY4e6yqVeNm", "KEEP"),
 ("34dVL1HaT63AXXGmioFrxMmcNYHCdJvaVyFtzsjFXK8M", "KEEP"),
 ("fNrJmJ1aQMx1vgnGwJcLWkUCBrDy7GF7ZpVqiXuRFrJ", "KEEP"),
 ("AY9wD7Fivmgbg1GadnFXbGGJ33u6mi2Qn2gbAyFy31mj", "REJECT"),
 ("3uJqqFxBRy5r3m4VeLRJJuvbUAuR7To9gkbvqRrQzYi4", "REJECT"),
 ("BKR2719GAki5oKSVHxFC2isrTbWBm4cfcbXpgEddEJTN", "REJECT"),
 ("E8DdM7FCX3hzf5XMQp9jhSe7S8L3d6NHjabuzNkAvXK", "KEEP"),
 ("7rBE5F3VA3x4DKmj28bEW2bsmEdi88EZ7MMnLWkm17Xh", "REJECT"),
 ("3WutnrpFUwUbQaQRM8KP7XATvpS31A4A7QDADfzYivfj", "REJECT"),
 ("CjzBQ2YSHviDB4XnjSCWvGJsujqFfsGrEahu1NRpouZy", "REJECT"),
 ("BrJH44GpEcfkM39NuEsZhvz5d8a2JkTJMYAG268Lb1Pq", "REJECT"),
 ("XppYcY1RZyDTLayChp87oyJtAE9azfTyfJTvv3YK45f", "KEEP"),
 ("ESZERcwH15AHRVyeapZer1xFQqb11JyhYuEjPukHvBge", "REJECT"),
 ("DsKmdkYx49Xc1WhqMUAztwhdYPTqieyC98VmnnJdgpXX", "REJECT"),
 ("C9cXPG5yk4cT8dfXwh3dfwetkU5Y1NvhN1MBNEFyD6mx", "REJECT"),
 ("76jqhDbvzmvE7vEH51zqv83wGJZDML5QFuB2i5UBUCX1", "REJECT"),
 ("G8DAdumet1M3SsRRPH6U8jbeeLyLGKxabVn3MssTvC47", "REJECT"),
 ("AqYP7WMXjqBKoa3uqDD7TkQp4yn6qpg57u3jwuFxxyqD", "KEEP"),
 ("EwevsUvU5YMHkTkWEpjNNFhDgfa9UzBWZnw5geALZGLn", "KEEP"),
 ("2dE65UdctQLscWCYEmmjHd1nVw3vKfnnFbwK2PqAC8NS", "REJECT"),
 ("HNfadZyU8CjEGHNBmq3UvH65JaMifnxv79gvTZdpLb86", "REJECT"),
 ("CYvf31AkSUw9UxkBZvzfxeKVtEwY3Qmz15B4JqXhG8YC", "REJECT"),
 ("Et2Q2HFRSGCMEL5TcjtRNbZ9bFSNzutkA399tNh1VjzF", "REJECT"),
 ("8TPWakvWw4xQbk7uAYdNjZiDKKHgv9GE5GebzsbtUaHr", "KEEP"),
]


def _be(path, tries=3):
    for _ in range(tries):
        try:
            r = urllib.request.Request("https://public-api.birdeye.so" + path,
                headers={"X-API-KEY": KEY, "x-chain": "solana", "Accept": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(r, timeout=25) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(4); continue
            return None
        except Exception:
            time.sleep(1.5)
    return None


def _pull_trades(wallet):
    now = int(time.time()); before = now; trades = []
    for _ in range(MAX_PAGES):
        b = _be(f"/trader/txs/seek_by_time?address={wallet}&before_time={before}&tx_type=swap&limit=100")
        time.sleep(0.18)
        data = (b or {}).get("data") or {}
        items = data.get("items") or []
        if not items:
            break
        for it in items:
            base = it.get("base") or {}
            ch = float(base.get("ui_change_amount") or 0)
            if ch == 0:
                continue
            tok = base.get("address")
            usd = abs(float(it.get("volume_usd") or 0))
            if not tok or usd < DUST:
                continue
            price = float(base.get("price") or 0) or (usd / abs(ch) if ch else 0)
            trades.append((int(it.get("block_unix_time") or 0), tok, ch, usd, price))
        oldest = min(int(it.get("block_unix_time") or now) for it in items)
        before = oldest - 1
        if not data.get("has_next"):
            break
    return trades, now


def _portfolio(wallet):
    d = (_be(f"/v1/wallet/token_list?wallet={wallet}") or {}).get("data") or {}
    held = {}
    for it in (d.get("items") or []):
        v = it.get("valueUsd") or it.get("value_usd") or it.get("value") or 0
        if it.get("address"):
            held[it["address"]] = float(v or 0)
    return held


def vet(wallet):
    trades, now = _pull_trades(wallet)
    if not trades:
        return None
    held = _portfolio(wallet)
    by = defaultdict(lambda: {"bq": 0.0, "bu": 0.0, "sq": 0.0, "su": 0.0,
                              "first": None, "last": None, "maxsell": 0.0})
    for (t, tok, ch, usd, px) in trades:
        d = by[tok]
        d["first"] = t if d["first"] is None else min(d["first"], t)
        d["last"] = t if d["last"] is None else max(d["last"], t)
        if ch > 0:
            d["bq"] += ch; d["bu"] += usd
        else:
            d["sq"] += abs(ch); d["su"] += usd
            d["maxsell"] = max(d["maxsell"], px)
    realized = unrealized = 0.0
    multix = multix500 = rugs = ntok = 0
    holds = []
    for tok, d in by.items():
        if d["bq"] <= 0:
            continue
        ntok += 1
        avg_cost = d["bu"] / d["bq"]
        realized += d["su"] - avg_cost * d["sq"]
        rem_q = max(0.0, d["bq"] - d["sq"])
        cur = held.get(tok, 0.0)
        unrealized += cur - avg_cost * rem_q
        if d["maxsell"] > 0 and avg_cost > 0:
            r = d["maxsell"] / avg_cost
            if r >= 2: multix += 1
            if r >= 6: multix500 += 1
        holds.append(d["last"] - d["first"])
        sold_cost = avg_cost * d["sq"]
        if (d["sq"] > 0 and d["su"] < 0.25 * sold_cost) or (rem_q > 0 and cur < 0.05 * avg_cost * rem_q):
            rugs += 1
    span_days = max(1e-6, (now - min(t for t, *_ in trades)) / 86400.0)
    tpd = len(trades) / span_days
    med_hold_min = (st.median(holds) / 60.0) if holds else 0.0
    rug_pct = 100.0 * rugs / ntok if ntok else 0.0
    net = realized + unrealized

    # ---- the QUALIFICATION screen ----
    if realized <= 0:
        v, why = "REJECT", "negative realized"
    elif unrealized < 0 and (-unrealized) > realized:
        v, why = "REJECT", "bag-holder (unrealized loss > realized)"
    elif multix < MULTIX_MIN:
        v, why = "REJECT", f"too few multi-x ({multix})"
    elif med_hold_min < TOO_FAST_MIN:
        v, why = "TOO_FAST", f"median hold {med_hold_min:.0f}m"
    elif realized < SIGNIF_USD:
        v, why = "REJECT", f"realized not significant (${realized:,.0f})"
    else:
        v, why = "KEEP", f"+${realized:,.0f} realized, {multix} multi-x"
    return dict(verdict=v, why=why, realized=realized, unrealized=unrealized, net=net,
                multix=multix, multix500=multix500, med_hold_min=med_hold_min,
                tpd=tpd, rug_pct=rug_pct, ntok=ntok, ntrades=len(trades))


# ---- ACCUMULATOR screen (2026-07-21) — distinct from the COPY-cluster screen above. -------
# Calibrated on the 6 held-accumulator candidates vetted by browser-Opus: the CdoQKour cluster
# taught us that "positive realized + multi-x + hold>15m" (the KEEP screen) passes profitable
# FLIPPERS/SNIPERS. The clean separator is FOCUS: real accumulators trade FEW tokens SLOWLY
# (7Tq 323 tok / 6 tpd, 4nB 91 / 3) while flippers/snipers spray (547-736 tok / 9-19 tpd) even
# when profitable. So gate on token-count + trades/day + real (>=5x) runners. n=6 calibration —
# widen/tighten as more browser-vetted wallets accrue.
ACCUM_MAX_NTOK = 400
ACCUM_MAX_TPD = 8.0
ACCUM_MIN_MULTIX500 = 3     # real 5x+ runners (not just 2x flips)
ACCUM_MIN_HOLD_MIN = 15.0


def screen_accumulator(m: dict) -> tuple[str, str]:
    """ACCUMULATOR verdict from vet() metrics. Returns (KEEP|REJECT, reason). A wallet is an
    accumulator only if it is FOCUSED (few tokens, slow) and has real held runners — not a
    profitable high-churn flipper/sniper. Coordination (shared-basket clusters) is handled
    separately by the caller."""
    if m["realized"] <= SIGNIF_USD:
        return "REJECT", f"realized ${m['realized']:,.0f} <= ${SIGNIF_USD:,.0f}"
    if m["unrealized"] < 0 and (-m["unrealized"]) > m["realized"]:
        return "REJECT", "bag-holder (unrealized loss > realized)"
    if m["ntok"] >= ACCUM_MAX_NTOK:
        return "REJECT", f"sprays {m['ntok']} tokens (>= {ACCUM_MAX_NTOK}) = flipper/sniper"
    if m["tpd"] >= ACCUM_MAX_TPD:
        return "REJECT", f"trades {m['tpd']:.0f}/day (>= {ACCUM_MAX_TPD}) = churn"
    if m["med_hold_min"] < ACCUM_MIN_HOLD_MIN:
        return "REJECT", f"median hold {m['med_hold_min']:.0f}m < {ACCUM_MIN_HOLD_MIN:.0f}m"
    if m["multix500"] < ACCUM_MIN_MULTIX500:
        return "REJECT", f"only {m['multix500']} real (>=5x) runners"
    return "KEEP", (f"+${m['realized']:,.0f}, {m['multix500']} runners, {m['ntok']} tok, "
                    f"{m['tpd']:.0f}/day, hold {m['med_hold_min']:.0f}m")


def portfolio_tokens(wallet: str, min_value_usd: float = 200.0) -> set:
    """The wallet's current holdings above a dust floor — for coordination/shared-basket
    detection. Uses /v1/wallet/token_list (works on our tier)."""
    return {tok for tok, v in _portfolio(wallet).items() if v >= min_value_usd}


def coordination_clusters(wallet_holdings: dict, min_shared: int = 3) -> list:
    """Group wallets that co-hold the same basket (a co-funded/coordinated cluster, e.g. the
    CdoQKour set that all held Jimothy + Tilly/Meowpin/Potato). `wallet_holdings` = {wallet:
    set(token_mints)}. Two wallets are linked if they share >= min_shared holdings; returns a
    list of clusters (each a list of wallets) via union-find. Independent skill = a singleton
    cluster; a multi-wallet cluster is coordination, keep at most one representative."""
    ws = list(wallet_holdings)
    parent = {w: w for w in ws}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(ws)):
        for j in range(i + 1, len(ws)):
            if len(wallet_holdings[ws[i]] & wallet_holdings[ws[j]]) >= min_shared:
                parent[find(ws[i])] = find(ws[j])
    groups = defaultdict(list)
    for w in ws:
        groups[find(w)].append(w)
    return list(groups.values())


def main():
    print(f"headless vet of {len(BATCH)} wallets (Lite raw-trade compute, {MAX_PAGES}-page cap) vs browser-Opus\n")
    hdr = f"{'#':>2} {'wallet':>10} {'MINE':>8} {'BO':>8} {'agree':>5} {'realized':>11} {'unrl':>9} {'mx':>3} {'holdMin':>7} {'tpd':>6} {'ntr':>5}"
    print(hdr)
    agree = disagree = 0
    diffs = []
    for i, (w, bo) in enumerate(BATCH, 1):
        r = vet(w)
        if r is None:
            print(f"{i:>2} {w[:10]:>10}   NO-DATA  (bo={bo})")
            continue
        ok = (r["verdict"] == bo)
        agree += ok; disagree += (not ok)
        if not ok:
            diffs.append((i, w, r, bo))
        print(f"{i:>2} {w[:10]:>10} {r['verdict']:>8} {bo:>8} {'OK' if ok else 'XXX':>5} "
              f"{r['realized']:>+11,.0f} {r['unrealized']:>+9,.0f} {r['multix']:>3} "
              f"{r['med_hold_min']:>7.0f} {r['tpd']:>6.0f} {r['ntrades']:>5}")
    print(f"\nAGREEMENT: {agree}/{agree+disagree} = {100*agree/max(1,agree+disagree):.0f}%")
    if diffs:
        print("\nDISAGREEMENTS (mine vs browser-Opus):")
        for i, w, r, bo in diffs:
            print(f"  #{i} {w[:12]}: mine={r['verdict']} ({r['why']}) | BO={bo} | "
                  f"real ${r['realized']:,.0f} unrl ${r['unrealized']:,.0f} mx{r['multix']} "
                  f"hold{r['med_hold_min']:.0f}m tpd{r['tpd']:.0f} ntr{r['ntrades']}")


if __name__ == "__main__":
    main()
