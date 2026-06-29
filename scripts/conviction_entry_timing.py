"""One-off: where in each token's life did conviction enter (adverse-selection check).

For each closed conviction paper trade, pull Birdeye token-creation + price history
and quantify: token age at our entry, how far it had ALREADY run before we entered
(run-up x), whether the token's peak came BEFORE our entry, and our max-favorable-
excursion after entry. Read-only. Run in the framework container.
"""
import asyncio, json as _json
import aiohttp
from datetime import timezone
from statistics import median
from sqlalchemy import text
from framework.db import session_scope
from bots.copy.config import get_copy_settings

BASE = "https://public-api.birdeye.so"

async def bget(session, path, params, key):
    h = {"X-API-KEY": key, "x-chain": "solana"}
    try:
        async with session.get(BASE+path, params=params, headers=h,
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
            t = await r.text()
            return (r.status, _json.loads(t) if r.status == 200 else None)
    except Exception:
        return (-1, None)

def u(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())

async def main():
    s = get_copy_settings()
    key = s.birdeye_api_key
    if not key:
        print("NO birdeye_api_key"); return 1
    with session_scope() as db:
        rows = db.execute(text("""
            SELECT asset, entry_at, exit_at, entry_price, pnl_pct,
                   left(sim_metadata->>'trigger_wallet',8) AS tw,
                   coalesce(sim_metadata->>'conviction_n_buys','?') AS nb
            FROM trades WHERE bot_id='copy' AND mode='paper'
              AND (sim_metadata->>'strategy')='conviction' AND fill_status='closed'
            ORDER BY entry_at
        """)).all()
    print(f"{len(rows)} conviction trades\n")
    hdr = f"{'token':10} {'trig':9} {'nb':>2} {'age_m':>6} {'runup':>6} {'pk<ent':>6} {'mfe%':>7} {'pnl%':>7}"
    print(hdr); print("-"*len(hdr))
    runups=[]; mfes=[]; pkbefore=0; cnt=0
    async with aiohttp.ClientSession() as session:
        for r in rows:
            mint=r.asset; ent=u(r.entry_at); ex=u(r.exit_at); ep=float(r.entry_price)
            st1,ci = await bget(session,"/defi/token_creation_info",{"address":mint},key)
            await asyncio.sleep(1.1)
            cre = (ci or {}).get("data",{}).get("blockUnixTime") if ci else None
            tf = (cre or (ent-21600))-1; tt = ex+3600
            gran = "5m" if (tt-tf) > 6*3600 else "1m"
            st2,hp = await bget(session,"/defi/history_price",
                {"address":mint,"address_type":"token","type":gran,"time_from":tf,"time_to":tt},key)
            await asyncio.sleep(1.1)
            items=((hp or {}).get("data",{}) or {}).get("items") or []
            if not items:
                print(f"{mint[:10]:10} {r.tw:9} {r.nb:>2} {'?':>6} {'?':>6} {'?':>6} {'?':>7} {r.pnl_pct:7.1f}  (no hist st={st1}/{st2})")
                continue
            pre=[it['value'] for it in items if it['unixTime']<=ent]
            post=[it['value'] for it in items if ent<it['unixTime']<=ex]
            premin=min(pre) if pre else ep
            runup=ep/premin if premin>0 else float('nan')
            mfe=(max(post)/ep-1)*100 if post and ep>0 else 0.0
            age=((ent-cre)/60) if cre else float('nan')
            pk=max(items,key=lambda x:x['value'])
            before = pk['unixTime']<=ent
            pkbefore += 1 if before else 0
            cnt+=1; runups.append(runup); mfes.append(mfe)
            print(f"{mint[:10]:10} {r.tw:9} {r.nb:>2} {age:6.0f} {runup:6.1f} {('YES' if before else 'no'):>6} {mfe:7.1f} {r.pnl_pct:7.1f}")
    if cnt:
        print(f"\nentered AFTER the token's peak: {pkbefore}/{cnt}")
        print(f"median run-up BEFORE our entry: {median([x for x in runups if x==x]):.1f}x")
        print(f"median MFE AFTER our entry:     {median([x for x in mfes if x==x]):.1f}%")
    return 0

raise SystemExit(asyncio.run(main()))