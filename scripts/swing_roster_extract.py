"""Extract the broadened swing roster from a swing_followcopy_bt.py result JSON.
Lists followable-positive wallets (the roster candidates) + summary stats. Offline.
Usage: python swing_roster_extract.py <swing_bt_broad.json> [min_n]
"""
import sys
import json

path = sys.argv[1] if len(sys.argv) > 1 else "reports/swing_copy_validation_2026-08-26/swing_bt_broad.json"
MIN_N = int(sys.argv[2]) if len(sys.argv) > 2 else 3

d = json.load(open(path))
pw = d["per_wallet"]
agg = d["agg"]
det = d["detail"]

pos = [(v["total"], w, v) for w, v in pw.items() if v["total"] > 0 and v["n"] >= MIN_N]
neg = [(v["total"], w, v) for w, v in pw.items() if v["total"] <= 0 and v["n"] >= MIN_N]
pos.sort(reverse=True)

print(f"AGG: N={agg['n']} positions, {agg['wallets']} wallets, "
      f"follow mean {agg['follow_mean']:+.1%}, total ${agg['follow_pnl']:+,.0f}, "
      f"mech ${agg['mech_pnl']:+,.0f}, follow-win {agg['follow_win']:.0%}")
print(f"\nFOLLOWABLE-POSITIVE (n>={MIN_N}): {len(pos)} wallets  |  negative: {len(neg)}  |  "
      f"total wallets w/ n>={MIN_N}: {len(pos)+len(neg)}")
print("-" * 78)
tot_pos = sum(t for t, _, _ in pos)
for t, w, v in pos:
    print(f"  {w:8}  n={v['n']:3d}  win {v['wins']:3d}/{v['n']:<3d} "
          f"mean {v['mean_ret']:+7.1%}  total ${t:+9,.0f}  crude_real ${v['crude_real']:+,.0f}")
print("-" * 78)
print(f"followable-positive total follow $ (n>={MIN_N}): ${tot_pos:+,.0f}")
print("\nprefixes for DB address lookup:")
print(",".join(f"'{w}'" for _, w, _ in pos))
