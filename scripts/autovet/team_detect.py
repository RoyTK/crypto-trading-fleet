import json
from collections import defaultdict

# edges: w1|w2|tight_cobuy_count
edges = []
for l in open("/home/fleet/pwmcp/tight_pairs.txt"):
    p = l.strip().split("|")
    if len(p) == 3:
        edges.append((p[0], p[1], int(p[2])))

# vet metrics (fresh session), if available
V = {}
for l in open("/home/fleet/pwmcp/fresh_vet_results.jsonl"):
    if l.strip():
        o = json.loads(l); V[o["address"]] = o

def components(min_edge):
    adj = defaultdict(set)
    for w1, w2, n in edges:
        if n >= min_edge:
            adj[w1].add(w2); adj[w2].add(w1)
    seen = set(); comps = []
    for node in adj:
        if node in seen: continue
        stack = [node]; comp = set()
        while stack:
            x = stack.pop()
            if x in seen: continue
            seen.add(x); comp.add(x); stack.extend(adj[x] - seen)
        if len(comp) >= 3:
            comps.append(comp)
    comps.sort(key=len, reverse=True)
    return comps

for thr in (3, 4):
    comps = components(thr)
    print(f"\n=== TEAMS at edge>=%d tight-co-buys (window 15min), size>=3: %d teams ===" % (thr, len(comps)))
    for i, c in enumerate(comps):
        vetted = sum(1 for a in c if a in V and V[a].get("verdict") == "KEEP")
        print(f"  Team {i+1} (size {len(c)}, {vetted} vetted-KEEP): " + ", ".join(a[:8] for a in sorted(c)))

# emit team defs at edge>=4 (cohesive core) + members file for enrichment
teams4 = components(4)
out = {"teams": [{"local_id": i + 1, "members": sorted(c)} for i, c in enumerate(teams4)]}
open("/home/fleet/pwmcp/teams_detected.json", "w").write(json.dumps(out, indent=2))
members = sorted({a for c in teams4 for a in c})
open("/home/fleet/pwmcp/team_members.txt", "w").write("\n".join(members) + "\n")
print(f"\nwrote teams_detected.json ({len(teams4)} teams at edge>=4) + team_members.txt ({len(members)} wallets)")
