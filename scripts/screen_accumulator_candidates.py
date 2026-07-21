"""Pre-screen held-accumulator candidates: profile each (reject flippers/snipers/insiders)
then collapse coordinated clusters to one representative. The pipeline's find-stage
(build_accumulator_roster_v2) surfaces wallets that co-hold runners — but the CdoQKour vet
proved that's mostly co-funded CLUSTERS of profitable flippers, not accumulator skill. This
stage adds the two things the finder can't see (2026-07-21): a full per-wallet PROFILE
(scripts.vet_wallets, reconstructed from /trader/txs + portfolio) and COORDINATION detection
(shared-basket clustering). Validated on the 6 browser-vetted wallets: it outputs exactly the
one genuine accumulator (7Tq4tSb7) that browser-Opus kept, rejecting the flipper/snipers/insider.

Usage (bot_copy):
  docker compose exec -T bot_copy python -m scripts.screen_accumulator_candidates <w1,w2,...>
  docker compose exec -T bot_copy python -m scripts.screen_accumulator_candidates --file wallets.json
Read-only. Feed it the candidate list from build_accumulator_roster_v2.
"""
import json
import sys

from scripts.vet_wallets import (
    vet, screen_accumulator, portfolio_tokens, coordination_clusters,
)


def screen(wallets: list[str]) -> dict:
    """Return {keep: [(wallet, metrics, why)], rejected: [(wallet, why)],
    clustered_out: [(wallet, kept_representative)]}. One representative per coordination
    cluster (the highest realized x runners)."""
    passed = {}   # wallet -> metrics
    rejected = []
    for w in wallets:
        m = vet(w)
        if not m:
            rejected.append((w, "no trade data"))
            continue
        verdict, why = screen_accumulator(m)
        if verdict == "KEEP":
            passed[w] = dict(m, _why=why)
        else:
            rejected.append((w, why))

    holdings = {w: portfolio_tokens(w) for w in passed}
    keep, clustered_out = [], []
    for cluster in coordination_clusters(holdings, min_shared=3):
        # representative = best realized * real-runners among the profile-passers in this cluster
        rep = max(cluster, key=lambda w: passed[w]["realized"] * max(1, passed[w]["multix500"]))
        keep.append((rep, passed[rep], passed[rep]["_why"]))
        for w in cluster:
            if w != rep:
                clustered_out.append((w, rep))
    return {"keep": keep, "rejected": rejected, "clustered_out": clustered_out}


def main():
    if len(sys.argv) < 2:
        print("usage: python -m scripts.screen_accumulator_candidates <w1,w2,...> | --file <json>")
        return
    if sys.argv[1] == "--file":
        data = json.load(open(sys.argv[2]))
        wallets = data["wallets"] if isinstance(data, dict) else data
    else:
        wallets = [w.strip() for w in sys.argv[1].split(",") if w.strip()]

    r = screen(wallets)
    print(f"screened {len(wallets)} candidates\n")
    print(f"=== KEEP (profile-passed accumulators, de-clustered): {len(r['keep'])} ===")
    for w, m, why in r["keep"]:
        print(f"  {w}  ({why})")
    print(f"\n=== rejected by profile: {len(r['rejected'])} ===")
    for w, why in r["rejected"]:
        print(f"  {w[:12]}  {why}")
    print(f"\n=== dropped as coordinated duplicates: {len(r['clustered_out'])} ===")
    for w, rep in r["clustered_out"]:
        print(f"  {w[:12]}  (co-funded with kept {rep[:12]})")


if __name__ == "__main__":
    main()
