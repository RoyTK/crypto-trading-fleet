"""Gate 2: short-window pop after tracked-wallet buys (fast-flip viability, coarse 1m).
Entry proxy = candle at the buy-minute => OPTIMISTIC upper bound (real entry ~13s in).
Read-only. Dedups to distinct (token, minute) signals; top-150 tokens by buy count.
"""
import asyncio, json as _json
import aiohttp
from statistics import median
from sqlalchemy import text
from framework.db import session_scope
from bots.copy.config import get_copy_settings

BASE="https://public-api.birdeye.so"; TOP=150; SLEEP=1.1

async def bget(session,path,params,key):
    h={"X-API-KEY":key,"x-chain":"solana"}
    try:
        async with session.get(BASE+path,params=params,headers=h,timeout=aiohttp.ClientTimeout(total=20)) as r:
            t=await r.text()
            return (r.status,_json.loads(t) if r.status==200 else None)
    except Exception:
        return (-1,None)

async def main():
    key=get_copy_settings().birdeye_api_key
    if not key: print("no key"); return 1
    with session_scope() as db:
        rows=db.execute(text("""
            SELECT token_mint,
                   array_agg(DISTINCT (floor(extract(epoch from event_at)/60)*60)::bigint) AS mins
            FROM wallet_swaps_log WHERE side='buy'
            GROUP BY token_mint ORDER BY count(*) DESC LIMIT :n
        """),{"n":TOP}).all()
    r1=[];r2=[];r3=[];mfe3=[]; n=0; nohist=0; e429=0
    async with aiohttp.ClientSession() as session:
        for row in rows:
            mint=row.token_mint; mins=sorted(set(row.mins))
            st,hp=await bget(session,"/defi/history_price",
                {"address":mint,"address_type":"token","type":"1m","time_from":min(mins)-180,"time_to":max(mins)+360},key)
            await asyncio.sleep(SLEEP)
            if st==429: e429+=1
            items=((hp or {}).get("data",{}) or {}).get("items") or []
            if not items: nohist+=1; continue
            items.sort(key=lambda x:x["unixTime"])
            T=[it["unixTime"] for it in items]; V=[it["value"] for it in items]
            for m in mins:
                ev=None
                for i in range(len(T)-1,-1,-1):
                    if T[i]<=m: ev=V[i]; break
                if not ev or ev<=0: continue
                def fwd(sec):
                    tgt=m+sec
                    for i in range(len(T)):
                        if T[i]>=tgt: return V[i]
                    return None
                mx=ev
                for i in range(len(T)):
                    if m<T[i]<=m+180: mx=max(mx,V[i])
                v1,v2,v3=fwd(60),fwd(120),fwd(180)
                if v1: r1.append(v1/ev-1)
                if v2: r2.append(v2/ev-1)
                if v3: r3.append(v3/ev-1)
                mfe3.append(mx/ev-1); n+=1
    def pct(xs,th): return 100*sum(1 for x in xs if x>=th)/len(xs) if xs else 0
    def neg(xs): return 100*sum(1 for x in xs if x<0)/len(xs) if xs else 0
    print(f"tokens={len(rows)} no_hist={nohist} http429={e429} signals(token-min)={n}\n")
    if n:
        print(f"MFE next 3min: median={median(mfe3)*100:5.1f}%   >=5%:{pct(mfe3,.05):4.0f}%   >=10%:{pct(mfe3,.10):4.0f}%   >=20%:{pct(mfe3,.20):4.0f}%")
        print(f"return @+1m: median={median(r1)*100:5.1f}%    @+2m:{median(r2)*100:5.1f}%    @+3m:{median(r3)*100:5.1f}%")
        print(f"frac below entry @+3m: {neg(r3):.0f}%")
        print("\nNOTE: optimistic upper bound (1m entry proxy). Subtract ~3-6% round-trip slippage for net.")
    return 0
raise SystemExit(asyncio.run(main()))