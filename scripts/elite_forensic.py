"""Phase 1b: forensic playbook of elite wallets (reverse-engineer entry strategy).
For each wallet's top WINNERS, reconstruct from Birdeye:
  - token AGE at their entry (first_buy - token_creation)  == "how early" (THE metric)
  - peak multiple after entry, and how much of the run they captured at exit
Answers: are they launch-snipers (age ~minutes -> uncapturable) or later-signal actors
(age hours -> potentially detectable), and what pattern they act on.
Read-only. Run inside bot_copy: docker compose exec -T bot_copy python scripts/elite_forensic.py
"""
import time, json, urllib.request, urllib.error
from statistics import median
from sqlalchemy import text
from framework.db import session_scope
from bots.copy.config import get_copy_settings

KEY = get_copy_settings().birdeye_api_key
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
TOP_WINNERS = 15
HORIZON_S = 7 * 24 * 3600

WALLETS = {
    "HCsfJh2q precision-sniper": "HCsfJh2qfGtsoJ9hkhBLyF84YMLFWFUcnTcKEZNtiFsW",
    "32Pwzbaz accumulator":      "32PwzbazNHCWN4CSrYAwuyLbkVajSDaRQCVPXmotMV8s",
    "8RAm2sFg moonshot-hunter":  "8RAm2sFg5CbbYmFBjcZaXWbwXad4TYRdt7jCPWU5bux7",
    "77n6X7Lt swing-holder":     "77n6X7LtGy5AZprsvjZu1eaekJpxqLeVRZLPJZdBYyg9",
}

WINNERS_SQL = """
SELECT token_mint,
  EXTRACT(EPOCH FROM MIN(event_at) FILTER (WHERE side='buy'))::bigint AS first_buy_u,
  EXTRACT(EPOCH FROM MAX(event_at) FILTER (WHERE side='sell'))::bigint AS last_sell_u,
  COALESCE(SUM(notional_usd) FILTER (WHERE side='buy'),0)  AS bought,
  COALESCE(SUM(notional_usd) FILTER (WHERE side='sell'),0) AS sold,
  COUNT(*) FILTER (WHERE side='buy') AS n_buys
FROM wallet_swaps_log WHERE wallet_address=:w
GROUP BY token_mint
HAVING COALESCE(SUM(notional_usd) FILTER (WHERE side='sell'),0)
     - COALESCE(SUM(notional_usd) FILTER (WHERE side='buy'),0) > 0
ORDER BY (COALESCE(SUM(notional_usd) FILTER (WHERE side='sell'),0)
        - COALESCE(SUM(notional_usd) FILTER (WHERE side='buy'),0)) DESC
LIMIT :lim
"""


def _be(path, tries=3):
    for _ in range(tries):
        try:
            r = urllib.request.Request("https://public-api.birdeye.so" + path,
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


def creation_unix(mint):
    d = _be(f"/defi/token_creation_info?address={mint}")
    v = ((d or {}).get("data") or {}).get("blockUnixTime")
    return int(v) if v is not None else None


def hist_1h(mint, t0, t1):
    h = _be(f"/defi/history_price?address={mint}&address_type=token&type=1H&time_from={t0}&time_to={t1}")
    items = ((h or {}).get("data") or {}).get("items") or []
    return sorted([(int(i["unixTime"]), float(i["value"])) for i in items if i.get("value")])


def price_at(series, t):
    prev = None
    for u, p in series:
        if u <= t: prev = p
        else: break
    return prev if prev is not None else (series[0][1] if series else None)


def pct_bucket(vals, edges):
    out = []
    for e in edges:
        out.append(sum(1 for v in vals if v is not None and v <= e))
    return out


def main():
    now = int(time.time())
    with session_scope() as s:
        for label, addr in WALLETS.items():
            rows = s.execute(text(WINNERS_SQL), {"w": addr, "lim": TOP_WINNERS}).all()
            ages_h, peak_mults, captures, entry_liq_ages = [], [], [], []
            detail = []
            for r in rows:
                mint = r.token_mint; fb = r.first_buy_u; ls = r.last_sell_u
                if not fb:
                    continue
                cu = creation_unix(mint); time.sleep(0.12)
                age_h = (fb - cu) / 3600.0 if cu else None
                series = hist_1h(mint, fb - 3600, min(now, fb + HORIZON_S)); time.sleep(0.12)
                entry_px = price_at(series, fb)
                fwd = [(u, p) for u, p in series if u >= fb]
                peak = max((p for _, p in fwd), default=None)
                peak_mult = (peak / entry_px) if (peak and entry_px and entry_px > 0) else None
                exit_px = price_at(series, ls) if ls else None
                # capture = fraction of the run (entry->peak) realized at exit
                cap = None
                if entry_px and peak and exit_px and peak > entry_px:
                    cap = (exit_px - entry_px) / (peak - entry_px)
                if age_h is not None: ages_h.append(age_h)
                if peak_mult is not None: peak_mults.append(peak_mult)
                if cap is not None: captures.append(cap)
                detail.append((mint[:6], age_h, peak_mult, cap, r.n_buys,
                               round(r.sold - r.bought)))
            print("=" * 82)
            print(f"{label}   ({len(detail)} winners enriched)")
            if ages_h:
                b = pct_bucket(ages_h, [1/6, 1, 6, 24])  # <=10min, <=1h, <=6h, <=24h
                n = len(ages_h)
                print(f"  AGE AT ENTRY (how early): median {median(ages_h):.2f}h  |  "
                      f"<=10min {b[0]}/{n}  <=1h {b[1]}/{n}  <=6h {b[2]}/{n}  <=24h {b[3]}/{n}")
            if peak_mults:
                print(f"  PEAK MULT after entry: median {median(peak_mults):.1f}x  "
                      f"max {max(peak_mults):.1f}x  (>=2x: {sum(1 for m in peak_mults if m>=2)}/{len(peak_mults)})")
            if captures:
                print(f"  EXIT CAPTURE (of entry->peak run): median {median(captures)*100:.0f}%")
            print("  token   age_h    peak_x   capture  n_buys   realized$")
            for tok, a, pm, c, nb, rz in detail:
                print(f"   {tok:6} {('%.2f'%a) if a is not None else '  ?':>7} "
                      f"{('%.1f'%pm) if pm else ' ?':>7} "
                      f"{('%.0f%%'%(c*100)) if c is not None else '  ?':>7} "
                      f"{nb:>6} {rz:>10}")


if __name__ == "__main__":
    main()
