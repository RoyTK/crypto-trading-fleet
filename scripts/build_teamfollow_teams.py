"""Rebuild team-follow teams from our own pre-run data (2026-11-18, Roy: "rebuild the teamfollow roster").

The 07-01 roster came from a Dune 90d corpus (co_buyer_teams.py, lost with an old scratchpad). By
November its biggest wallets had stopped trading on-chain and only 1 team was still active. This
rebuilds teams from `prerun_accumulators` (the daily runner scraper: runner tokens + each early
buyer's first buy), using the original rule:

  pair (A, B) = both bought >= MIN_SHARED of the same runners with first buys <= WINDOW_S apart,
                and shared / min(runners_A, runners_B) >= MIN_OVERLAP
  team        = connected component of linked pairs (size 2..MAX_TEAM)

Liveness is checked ON-CHAIN (Helius getSignaturesForAddress): a member counts if it made a successful
tx in the last LIVE_DAYS days; a team needs >= 2 live members (dormant teams were the November
failure). Members above MAX_TX_PER_DAY successful txs/day are dropped (spray bots; webhook-credit hogs). Members already on another tier (active/watch) are left out because a
wallet has one tier and only 'teamfollow'-tier wallets feed the team detector; teams that drop below
2 members are skipped. Wallets already on the roster are skipped (no double membership).

New teams start on WATCH (probation, Roy 2026-10-01): they trade the isolated 'teamfollow_watch'
track and are promoted by teamfollow_team_tiers only once forward net > 0 (robust, ex-best).

Run (framework container):
  python -m scripts.build_teamfollow_teams            # dry run: print candidate teams
  python -m scripts.build_teamfollow_teams --apply    # roster JSON + wallet_pool + team status + webhook
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

from framework.db import session_scope

LOOKBACK_DAYS = 90
WINDOW_S = 600
MIN_SHARED = 3
MIN_OVERLAP = 0.35
RUNNERS_MIN, RUNNERS_MAX = 3, 60       # wallets on > 60 runners are spray bots
MAX_TEAM = 12
LIVE_DAYS = 7
MAX_TX_PER_DAY = 1000
NEW_TEAM_ID_BASE = 2000
ROSTER = Path(__file__).resolve().parent.parent / "bots" / "copy" / "teamfollow_roster.json"
HELIUS = "https://api.helius.xyz/v0/webhooks"


def load_rows():
    with session_scope() as s:
        return s.execute(text("""
            SELECT DISTINCT ON (wallet, token) wallet, token, first_buy
            FROM prerun_accumulators
            WHERE run_date > now() - make_interval(days => :d) AND first_buy IS NOT NULL
            ORDER BY wallet, token, first_buy"""), {"d": LOOKBACK_DAYS}).all()


def build_teams(rows):
    by_wallet, by_token = {}, {}
    for w, t, fb in rows:
        by_wallet.setdefault(w, {})[t] = fb
        by_token.setdefault(t, []).append((w, fb))
    ok = {w for w, toks in by_wallet.items() if RUNNERS_MIN <= len(toks) <= RUNNERS_MAX}
    shared = {}
    for t, buys in by_token.items():
        buys = [(w, fb) for w, fb in buys if w in ok]
        buys.sort(key=lambda x: x[1])
        for i, (a, fa) in enumerate(buys):
            for b, fbb in buys[i + 1:]:
                if (fbb - fa).total_seconds() > WINDOW_S:
                    break
                key = (a, b) if a < b else (b, a)
                shared[key] = shared.get(key, 0) + 1
    links = [(a, b, n) for (a, b), n in shared.items()
             if n >= MIN_SHARED and n / min(len(by_wallet[a]), len(by_wallet[b])) >= MIN_OVERLAP]
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b, _ in links:
        parent[find(a)] = find(b)
    groups = {}
    for w in parent:
        groups.setdefault(find(w), set()).add(w)
    co = {}
    for a, b, n in links:
        r = find(a)
        co[r] = max(co.get(r, 0), n)
    teams = []
    for r, members in groups.items():
        last = {w: max(by_wallet[w].values()) for w in members}
        teams.append({"members": sorted(members), "co_runners": co.get(r, 0), "last_buy": last})
    return teams


def onchain_activity(wallet: str, key: str) -> tuple[int, float]:
    """(successful txs in the last LIVE_DAYS days, successful txs/day over that span; capped at 2 pages)."""
    import time
    url = f"https://mainnet.helius-rpc.com/?api-key={key}"
    cut = time.time() - LIVE_DAYS * 86400
    n, before = 0, None
    for _ in range(2):
        p = {"limit": 1000}
        if before:
            p["before"] = before
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                           "params": [wallet, p]}).encode()
        res = json.loads(urllib.request.urlopen(urllib.request.Request(
            url, body, {"Content-Type": "application/json"}), timeout=60).read()).get("result") or []
        done = False
        for x in res:
            if (x.get("blockTime") or 0) < cut:
                done = True
                break
            n += x.get("err") is None
        if done or len(res) < 1000:
            break
        before = res[-1]["signature"]
    return n, n / LIVE_DAYS


def main():
    apply = "--apply" in sys.argv
    teams = build_teams(load_rows())
    roster = json.loads(ROSTER.read_text())
    on_roster = {w for t in roster["teams"] for w in t["members"]}
    with session_scope() as s:
        tier = dict(s.execute(text("SELECT address, tier FROM wallet_pool")).all())
    key = os.environ["HELIUS_API_KEY"]
    picked, skipped = [], {"too_big": 0, "dormant": 0, "lt2_free": 0, "bot_members": 0}
    for t in sorted(teams, key=lambda t: (-t["co_runners"], -len(t["members"]))):
        if len(t["members"]) > MAX_TEAM:
            skipped["too_big"] += 1
            print(f"  (skipped oversized component: {len(t['members'])} wallets, co_runners {t['co_runners']})")
            continue
        free = [w for w in t["members"] if w not in on_roster and tier.get(w) in (None, "pruned")]
        act = {w: onchain_activity(w, key) for w in free}
        bots = [w for w in free if act[w][1] > MAX_TX_PER_DAY]
        free = [w for w in free if w not in bots]
        live = [w for w in free if act[w][0] > 0]
        if bots:
            skipped["bot_members"] += len(bots)
        if len(free) < 2:
            skipped["lt2_free"] += 1
            continue
        if len(live) < 2:
            skipped["dormant"] += 1
            continue
        picked.append({"members": free, "co_runners": t["co_runners"], "live": len(live),
                       "last": max(t["last_buy"][w] for w in free),
                       "tx_day": round(sum(act[w][1] for w in free))})
    next_id = max([NEW_TEAM_ID_BASE - 1] + [t["team_id"] for t in roster["teams"]]) + 1
    for i, t in enumerate(picked):
        t["team_id"] = next_id + i
    n_w = sum(len(t["members"]) for t in picked)
    print(f"teams found {len(teams)}; picked {len(picked)} ({n_w} wallets); skipped {skipped}")
    for t in picked:
        print(f"  team {t['team_id']}: size {len(t['members'])} live {t['live']} co_runners {t['co_runners']}"
              f" runner-buy {t['last']:%m-%d} tx/day {t['tx_day']}  " + ",".join(w[:8] for w in t["members"]))
    if not apply or not picked:
        return
    today = datetime.now(timezone.utc).date().isoformat()
    for t in picked:
        roster["teams"].append({"team_id": t["team_id"], "members": t["members"], "size": len(t["members"]),
                                "co_runners": t["co_runners"],
                                "note": f"{today} rebuild from prerun_accumulators (90d, <= {WINDOW_S}s co-buy, "
                                        f"overlap >= {MIN_OVERLAP}); starts on WATCH (probation)"})
    roster["_meta"]["n_teams"] = len(roster["teams"])
    roster["_meta"]["n_wallets"] = len({w for t in roster["teams"] for w in t["members"]})
    roster["_meta"]["rebuild_2026_11_18"] = f"+{len(picked)} teams / +{n_w} wallets via scripts/build_teamfollow_teams.py"
    ROSTER.write_text(json.dumps(roster, indent=1))
    new_wallets = sorted({w for t in picked for w in t["members"]})
    with session_scope() as s:
        for w in new_wallets:
            s.execute(text("""
                INSERT INTO wallet_pool (address, chain, tier, source, added_at)
                VALUES (:w, 'solana', 'teamfollow', 'teamfollow_rebuild_20261118', now())
                ON CONFLICT (address) DO UPDATE SET tier = 'teamfollow', source = 'teamfollow_rebuild_20261118',
                    pinned = false"""), {"w": w})
    from bots.copy.loop_helpers import ensure_team_status_table, set_teamfollow_team_status
    ensure_team_status_table()
    for t in picked:
        set_teamfollow_team_status(t["team_id"], "watch", "probation: new team (rebuild 2026-11-18)")
    # add to the teamfollow webhook (fetch by id: the list endpoint omits addresses)
    k = key
    for h in json.loads(urllib.request.urlopen(f"{HELIUS}?api-key={k}").read()):
        if not h["webhookURL"].endswith("/teamfollow"):
            continue
        f = json.loads(urllib.request.urlopen(f"{HELIUS}/{h['webhookID']}?api-key={k}").read())
        addrs = sorted(set(f["accountAddresses"]) | set(new_wallets))
        body = {"webhookURL": f["webhookURL"], "transactionTypes": f["transactionTypes"],
                "accountAddresses": addrs, "webhookType": f["webhookType"], "authHeader": f.get("authHeader"),
                "txnStatus": f.get("txnStatus") or "success"}
        req = urllib.request.Request(f"{HELIUS}/{h['webhookID']}?api-key={k}", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"}, method="PUT")
        out = json.loads(urllib.request.urlopen(req).read())
        print(f"teamfollow webhook: {len(f['accountAddresses'])} -> {len(out.get('accountAddresses') or [])} addresses")
    print(f"applied: {len(picked)} teams on watch, {len(new_wallets)} wallets -> tier teamfollow")


if __name__ == "__main__":
    main()
