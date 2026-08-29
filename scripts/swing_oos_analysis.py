"""Out-of-sample + slippage stress on the swing-copy backtest detail (reads swing_bt.json).
Attacks the two risks that could be fooling the headline: in-sample wallet selection, and
optimistic tail-exit fills. Offline (no Birdeye). Usage: python swing_oos_analysis.py <json>
"""
import sys
import json
from datetime import datetime, timezone

FEE = 0.025  # fee baked into stored 'follow'/'mech' -> raw = stored + FEE
SIZE = 500.0


def med(x):
    x = sorted(x); k = len(x)
    return 0.0 if not k else (x[k // 2] if k % 2 else (x[k // 2 - 1] + x[k // 2]) / 2)


def dt(u):
    return datetime.fromtimestamp(u, timezone.utc).strftime("%m-%d")


def block(name, dets):
    n = len(dets)
    if not n:
        print(f"  {name:26} n=0"); return
    f = [d["follow"] for d in dets]; m = [d["mech"] for d in dets]
    wins = sum(1 for r in f if r > 0)
    print(f"  {name:26} n={n:3d}  FOLLOW ${SIZE*sum(f):+8,.0f} (mean {sum(f)/n:+.1%}, med {med(f):+.1%}, "
          f"win {wins/n:.0%})  MECH ${SIZE*sum(m):+8,.0f}  edge {SIZE*(sum(f)-sum(m)):+8,.0f}")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "reports/swing_copy_validation_2026-08-26/swing_bt.json"
    d = json.load(open(path))
    det = [x for x in d["detail"] if "open_t" in x]
    if not det:
        print("no open_t in detail — re-run backtest with the open_t patch."); return
    det.sort(key=lambda x: x["open_t"])
    n = len(det)
    cut = det[n // 2]["open_t"]  # median open time -> balanced split
    h1 = [x for x in det if x["open_t"] < cut]
    h2 = [x for x in det if x["open_t"] >= cut]
    print("=" * 96)
    print(f"OOS + SLIPPAGE STRESS  (N={n}, split at {dt(cut)}; H1 {dt(det[0]['open_t'])}..{dt(cut)}, "
          f"H2 {dt(cut)}..{dt(det[-1]['open_t'])})")

    print("\n[1] AGGREGATE TIME-STABILITY  (does follow>mech hold in BOTH halves?)")
    block("H1 (early)", h1)
    block("H2 (late)", h2)

    print("\n[2] WALLET-SELECTION OOS  (rank wallets on H1, test the picks on H2)")
    # per-wallet H1 / H2 follow sums
    wh1, wh2 = {}, {}
    for x in h1:
        wh1.setdefault(x["w"], []).append(x)
    for x in h2:
        wh2.setdefault(x["w"], []).append(x)
    # SELECTED = H1 follow-sum > 0 with >=3 H1 positions
    selected = {w for w, xs in wh1.items() if len(xs) >= 3 and sum(v["follow"] for v in xs) > 0}
    rejected = {w for w in wh1 if w not in selected and len(wh1[w]) >= 3}
    sel_h2 = [x for x in h2 if x["w"] in selected]
    rej_h2 = [x for x in h2 if x["w"] in rejected]
    print(f"  selected {len(selected)} wallets on H1 (follow>0, >=3 pos); rejected {len(rejected)}")
    block("SELECTED -> H2", sel_h2)
    block("REJECTED -> H2", rej_h2)
    # per-wallet H1 vs H2 sign agreement
    both = [w for w in selected | rejected if w in wh2 and len(wh2[w]) >= 2]
    if both:
        agree = sum(1 for w in both
                    if (sum(v["follow"] for v in wh1[w]) > 0) == (sum(v["follow"] for v in wh2[w]) > 0))
        print(f"  per-wallet H1/H2 follow-sign agreement: {agree}/{len(both)} ({agree/len(both):.0%}) "
              f"(wallets with >=3 H1 and >=2 H2 positions)")

    print("\n[3] SLIPPAGE / FEE SENSITIVITY  (how much cost can the edge absorb?)")
    raw = [x["follow"] + FEE for x in det]  # gross follow return
    for extra in (0.025, 0.05, 0.10, 0.15):
        net = [r - extra for r in raw]
        tot = SIZE * sum(net); wins = sum(1 for r in net if r > 0)
        # tail dependence at this cost
        s = sorted(net); drop2 = SIZE * sum(s[:int(n * 0.98)])
        print(f"  round-trip cost {extra:4.1%}: total ${tot:+8,.0f}  mean {sum(net)/n:+.1%}  "
              f"win {wins/n:.0%}   (drop top 2%: ${drop2:+,.0f})")
    print("  note: a FLAT cost barely dents +100-900% tail winners; real risk is thin-book PRICE")
    print("        IMPACT on those exits, which a flat % can't capture -> liquidity-aware paper fills.")
    print("=" * 96)


if __name__ == "__main__":
    main()
