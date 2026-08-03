import json, statistics as st
from collections import defaultdict

# --- load vet metrics ---
V = {}
for l in open("/home/fleet/pwmcp/fresh_vet_results.jsonl"):
    if l.strip():
        o = json.loads(l)
        V[o["address"]] = o

# --- load prerun profile: wallet|n_runners|avg_nbuys|max_runx|avg_runx|avg_lead ---
P = {}
for l in open("/home/fleet/pwmcp/fresh_profile.txt"):
    p = l.strip().split("|")
    if len(p) >= 6:
        P[p[0]] = dict(n_runners=int(p[1]), avg_nbuys=float(p[2]),
                       max_runx=float(p[3]), avg_runx=float(p[4]), avg_lead=float(p[5]))

# --- classify ---
def classify(addr):
    m = V.get(addr); pr = P.get(addr, {})
    if not m or m.get("verdict") == "ERROR" or m.get("realized") is None:
        return "SKIP_NODATA", "no metrics"
    rz = m.get("realized") or 0
    un = m.get("unrealized")
    tx = m.get("txns") or 0
    ins = m.get("instant_sell")
    mx = m.get("multi_x", 0)
    rug = m.get("scam_rug")
    if rz <= 0: return "EXCLUDE", "unprofitable"
    if tx > 1_000_000: return "EXCLUDE_MM", f"mm-suspect {tx:,.0f} txns"
    if ins is not None and ins >= 50: return "EXCLUDE_FAST", f"instant-sell {ins}%"
    if un is not None and un < 0 and abs(un) > rz: return "EXCLUDE_BAG", "bag-holder"
    if rz >= 5000 and mx >= 3: return "CLUSTER_A", "active-worthy"
    return "CLUSTER_B", "cosigner (modest PnL, recurring)"

def is_conviction(addr):
    m = V.get(addr); pr = P.get(addr, {})
    if not m: return False
    rz = m.get("realized") or 0; mx = m.get("multi_x", 0); rug = m.get("scam_rug")
    ins = m.get("instant_sell")
    return (pr.get("avg_nbuys", 0) >= 5 and rz >= 5000 and mx >= 3
            and (rug is None or rug < 70) and not (ins is not None and ins >= 50))

tiers = defaultdict(list)
conv = []
for addr in P:
    t, why = classify(addr)
    tiers[t].append(addr)
    if t in ("CLUSTER_A", "CLUSTER_B") and is_conviction(addr):
        conv.append(addr)

print("=== TIER COUNTS (of %d fresh recurring wallets) ===" % len(P))
for t in ["CLUSTER_A", "CLUSTER_B", "EXCLUDE", "EXCLUDE_BAG", "EXCLUDE_FAST", "EXCLUDE_MM", "SKIP_NODATA"]:
    print(f"  {t:14} {len(tiers[t])}")
print(f"  CONVICTION-tagged (subset of A/B): {len(conv)}")

def show(addr):
    m = V[addr]; pr = P[addr]
    return (f"{addr[:12]:12} R${(m.get('realized') or 0):>10,.0f} U${(m.get('unrealized') or 0):>9,.0f} "
            f"mx{m.get('multi_x',0):>5} rug{(m.get('scam_rug') or 0):>5.0f}% ins{('--' if m.get('instant_sell') is None else str(m.get('instant_sell')) )} "
            f"| runners{pr['n_runners']:>3} nbuys{pr['avg_nbuys']:>5} maxX{pr['max_runx']:>7,.0f}")

print("\n=== CLUSTER_A (active-worthy), top 30 by realized ===")
for a in sorted(tiers["CLUSTER_A"], key=lambda a: -(V[a].get("realized") or 0))[:30]:
    print("  " + show(a))
print(f"  ... {len(tiers['CLUSTER_A'])} total")

print("\n=== CONVICTION candidates (accumulators, lower-rug, profitable) ===")
for a in sorted(conv, key=lambda a: -P[a]["avg_nbuys"]):
    print("  " + show(a))

# --- team components among keep-worthy (A+B), edges = pairs sharing >=5 runners ---
keep = set(tiers["CLUSTER_A"]) | set(tiers["CLUSTER_B"])
adj = defaultdict(set)
for l in open("/home/fleet/pwmcp/team_pairs.txt"):
    p = l.strip().split("|")
    if len(p) == 3 and int(p[2]) >= 5 and p[0] in keep and p[1] in keep:
        adj[p[0]].add(p[1]); adj[p[1]].add(p[0])
seen = set(); comps = []
for n in adj:
    if n in seen: continue
    stack=[n]; comp=set()
    while stack:
        x=stack.pop()
        if x in seen: continue
        seen.add(x); comp.add(x)
        stack.extend(adj[x]-seen)
    if len(comp) >= 3: comps.append(comp)
comps.sort(key=len, reverse=True)
print(f"\n=== TEAM components among keep-worthy (edge=>=5 shared runners), size>=3: {len(comps)} teams ===")
for i, c in enumerate(comps):
    print(f"  Team {i+1} (size {len(c)}): " + ", ".join(a[:8] for a in sorted(c)))

# --- write apply lists ---
open("/home/fleet/pwmcp/cluster_A.txt","w").write("\n".join(tiers["CLUSTER_A"])+"\n")
open("/home/fleet/pwmcp/cluster_B.txt","w").write("\n".join(tiers["CLUSTER_B"])+"\n")
open("/home/fleet/pwmcp/conviction_cand.txt","w").write("\n".join(conv)+"\n")
print(f"\nwrote cluster_A.txt ({len(tiers['CLUSTER_A'])}), cluster_B.txt ({len(tiers['CLUSTER_B'])}), conviction_cand.txt ({len(conv)})")
