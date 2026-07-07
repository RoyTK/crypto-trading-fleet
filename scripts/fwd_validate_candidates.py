"""Forward-validate the GENUINE (non-bot) recurrence_candidates — INCL DUDS.

Following an accumulator only has edge if their buys ACROSS THE BOARD (winners AND
duds) are net-positive. The prerun corpus is runners-only (survivorship), so we test
each candidate wallet's OWN full recent buy history — every buy, not just the pre-run
hits that landed them on the recurrence list. Same discipline that killed the
recurrence-bot candidates and fast-flip.

Reads wallets from `recurrence_candidates WHERE NOT is_bot`. Read-only (Birdeye +
DB SELECT). Prints a per-wallet verdict table. Run server-side (has the Birdeye key).
"""
import time, json, urllib.request, urllib.error, statistics as st
from sqlalchemy import text
from framework.db import session_scope
from bots.copy.config import get_copy_settings

KEY = get_copy_settings().birdeye_api_key
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"
DUST = 50.0
FWD_DAYS = 7            # forward outcome window per buy
SAMPLE_MIN_AGE = 7      # buys must be >= 7d old (so the 7d forward window is complete)
SAMPLE_MAX_AGE = 45     # ...and <= 45d old (recent-ish behaviour)
MAX_BUYS = 40           # cap per wallet
now = int(time.time())


def be(path):
    r = urllib.request.Request("https://public-api.birdeye.so" + path,
        headers={"X-API-KEY": KEY, "x-chain": "solana", "Accept": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(r, timeout=25) as resp:
        return json.loads(resp.read().decode())


def be_try(path, tries=3):
    for i in range(tries):
        try:
            return be(path)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(4); continue
            return None
        except Exception:
            time.sleep(1.5)
    return None


def wallet_buys(wallet):
    """Distinct-ish recent buys (dedust) aged SAMPLE_MIN_AGE..SAMPLE_MAX_AGE days."""
    before = now - SAMPLE_MIN_AGE * 86400
    lo = now - SAMPLE_MAX_AGE * 86400
    out = []
    for _ in range(8):
        b = be_try(f"/trader/txs/seek_by_time?address={wallet}"
                   f"&before_time={before}&tx_type=swap&limit=100")
        time.sleep(0.25)
        items = ((b or {}).get("data") or {}).get("items") or []
        if not items:
            break
        for it in items:
            base = it.get("base") or {}
            t = int(it.get("block_unix_time") or 0)
            if float(base.get("ui_change_amount") or 0) > 0:   # a buy of base
                usd = abs(float(it.get("volume_usd") or 0))
                amt = abs(float(base.get("ui_change_amount") or 0))
                price = float(base.get("price") or 0) or (usd / amt if amt else 0)
                tok = base.get("address")
                if usd >= DUST and lo <= t <= (now - SAMPLE_MIN_AGE * 86400) and tok and price:
                    out.append((t, tok, usd, price))
        oldest = min(int(it.get("block_unix_time") or now) for it in items)
        if oldest < lo:
            break
        before = oldest - 1
    return out


def forward_outcome(tok, buy_t, buy_px):
    """MFE mult + end-of-window mult over FWD_DAYS after the buy."""
    hp = be_try(f"/defi/history_price?address={tok}&address_type=token&type=1H"
                f"&time_from={buy_t}&time_to={buy_t + FWD_DAYS * 86400}")
    time.sleep(0.2)
    pts = [float(p["value"]) for p in (((hp or {}).get("data") or {}).get("items") or []) if p.get("value")]
    if not pts:
        return None
    return max(pts) / buy_px, pts[-1] / buy_px


def med(xs):
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else float("nan")


with session_scope() as s:
    wallets = [(r[0], r[1], r[2], r[3]) for r in s.execute(text("""
        SELECT wallet, runners, round(trades_per_day::numeric,0), round(avg_lead_days::numeric,1)
        FROM recurrence_candidates WHERE NOT is_bot AND status='candidate' ORDER BY runners DESC, avg_usd DESC
    """)).fetchall()]

print(f"forward-validating {len(wallets)} genuine candidates INCL DUDS "
      f"(buys {SAMPLE_MIN_AGE}-{SAMPLE_MAX_AGE}d old, {FWD_DAYS}d forward window)\n")
print(f"{'wallet':>44} {'run':>3} {'tpd':>4} {'lead':>4} {'nBuys':>5} {'ran2x':>6} "
      f"{'ran5x':>6} {'rug':>5} {'medMFE':>6} {'hold7dEV':>8}")
for w, runners, tpd, lead in wallets:
    buys = wallet_buys(w)
    outs = []
    for (t, tok, usd, px) in buys[:MAX_BUYS]:
        o = forward_outcome(tok, t, px)
        if o:
            outs.append(o)
    n = len(outs)
    if n == 0:
        print(f"{w:>44} {runners:>3} {tpd:>4} {lead:>4} {0:>5}  (no forward data)")
        continue
    ran2 = sum(1 for mfe, _ in outs if mfe >= 2) / n
    ran5 = sum(1 for mfe, _ in outs if mfe >= 5) / n
    rug = sum(1 for _, end in outs if end <= 0.2) / n
    medmfe = med([mfe for mfe, _ in outs])
    ev = st.mean([end - 1 for _, end in outs])   # hold-7d EV incl duds (follow every buy)
    print(f"{w:>44} {runners:>3} {tpd:>4} {lead:>4} {n:>5} {100*ran2:>5.0f}% "
          f"{100*ran5:>5.0f}% {100*rug:>4.0f}% {medmfe:>6.2f} {100*ev:>+7.0f}%")

print("\nRead: EDGE if hold7dEV clearly positive AND ran2x/ran5x beat base + rug tolerable.")
print("n<~15 = underpowered (thin, a wallet on only 3 runners); treat as a first read.")
