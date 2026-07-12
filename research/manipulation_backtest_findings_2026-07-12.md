# Manipulation-Detector Backtest vs Our Own Trades — Findings

_2026-07-12. Detectors: `scripts/manipulation_detectors.py` (wash-score, zero-risk,
circular, LPI — from "A Midsummer Meme's Dream" + "Resisting Manipulative Bots in
Memecoin Copy Trading"). Harness: `scripts/research_manip_backtest.py`. Sample: 266
distinct tokens from our closed COPY paper trades (cluster 144 / conviction 62 /
teamfollow 60; all had ≥5 window txns). Calibration, not a go/no-go — the papers'
N (34,988 tokens) dwarfs ours; Roy is building on these regardless._

## Result 1 — the paper thresholds don't transfer (calibrate, as expected)

The Paper-2 binary wash-score flag (threshold 50) fires on **98–100%** of our tokens
(near-zero net position explodes the round-trip/net ratio). Useless as-is. **Use the
continuous `zero_risk_vol_frac`** (share of window volume from makers who buy then sell
~equal size, within 2%) — that one discriminates.

## Result 2 — wash trading marks LOSERS, and runners have CLEAN volume ★

Window `[entry−6h, entry+24h]`:

| Split | group | zero-risk-flagged (≥0.5) | median zero-risk frac |
|---|---|---|---|
| **Q1 run outcome** | dud (peak<100%, n=241) | 13% | 0.15 |
| | **runner (peak≥100%, n=25)** | **0%** | 0.14 |
| **Q2 our outcome** | **our loss (n=182)** | **16%** | **0.20** |
| | **our win (n=84)** | **2%** | **0.11** |
| by strategy | conviction (n=62, worst PnL) | 23% | **0.31** |
| | cluster (n=144) | 9% | 0.13 |
| | teamfollow (n=60) | 7% | 0.12 |

**Two clean signals:**
1. **Zero-risk wash separates our losers from winners 8:1 (16% vs 2%).** Tokens with
   heavy fake round-trip volume in our window were far more often losses.
2. **Runners are NOT wash-heavy (0/25 flagged).** Real runs have real broad volume →
   avoiding wash is a **"free" filter**: it cuts losers without costing runners. The
   usual manipulation paradox (avoid manipulation = avoid winners) does NOT apply to
   *wash* specifically. This is the key result.
3. Our worst strategy (conviction, −10% avg) is the most wash-heavy (med 0.31) —
   consistent with it following wallets into fake-volume tokens.

LPI: 0% across the board — expected; our $50k liquidity floor already excludes the
thin-pool tokens LPI targets. Circular-volume: muddy (loss 12% vs win 19%), not usable.

## The validity caveat this run can't resolve

The `[entry−6h, +24h]` window includes **post-entry** activity, so the Result-2 wash
could be the DUMP happening while we lost, not a signal available *before* entry. That
makes it ambiguous between:
- an **entry filter** (skip tokens already wash-heavy pre-entry), or
- an **exit/during-hold signal** (wash-onset = we're being dumped on → exit).

## Result 3 — strict pre-entry `[entry−12h, entry]` CORRECTS Result 2 (the honest verdict)

_(A float `before_time` bug first returned a false 0/266; fixed `4ca809c`. Real run:
266/266 covered — every token has pre-entry history.)_

Median zero-risk fraction, pre-entry → full-window:

| bucket | pre-entry | full-window | post-entry adds |
|---|---|---|---|
| our loss | 0.115 | 0.199 | +0.084 |
| our win | 0.060 | 0.110 | +0.050 |
| runner | 0.062 | 0.144 | +0.082 |

**What this changes:** the eye-catching full-window "8:1, free filter" from Result 2 was
**inflated by post-entry activity + the binary ≥0.5 threshold catching the DUMP on
losers.** Corrected:
1. **Pre-entry, losers carry ~2× the wash of winners/runners (0.115 vs 0.060)** — a real
   but weak tilt.
2. **It is NOT a free entry filter.** A pre-entry `zero_risk ≥ T` gate skips losers and
   runners at nearly the same rate (T=0.15 → skip 37% losers / 29% wins / **36% runners**;
   T=0.10 → 55% / 38% / 36%). For a positive-skew strategy where 3 runners carry the PnL,
   sacrificing ~a third of runners to cut ~a third of losers is **likely EV-negative**.
   My earlier "free filter" call was an artifact — walked back.
3. **Runners get washed post-entry too** (+0.082), so post-entry wash doesn't cleanly
   separate runner-from-loss either.

## Verdict

- **Entry filter (tested): WEAK, not worth shipping standalone** — cuts runners ≈ as much
  as losers. LPI dead (liq floor covers it); circular-volume = noise; paper wash-flag
  (thr 50) useless (fires ~always).
- **Exit-timing signal (UNTESTED): still the most promising use.** The papers' core
  finding — manipulation *precedes* extraction — is about the *sequence* (wash-onset →
  dump), which needs an intra-hold **timeline** (did wash spike BEFORE the local top?).
  This backtest only measured aggregate window wash, so the exit use is open, not negative.
- Consistent with the session pattern: an exciting first read washed out under
  forward/validity rigor (cf. fast-flip, slow-cluster). Measure-first earned its keep by
  catching my own over-call.

## Where this goes (Roy: build/adapt regardless)

1. **Don't ship wash as a standalone gate.** Instead **instrument**: log the detector
   feature-set (zero_risk, circular, wash_score, LPI) on every LIVE signal + outcome →
   builds a labeled dataset so a *multi-factor* model can be tested (Paper 2 got 73%
   precision from the COMBINATION of tx-metrics + candles + sentiment, never one detector).
2. **If chasing the exit signal:** build the intra-hold wash-onset-vs-peak timeline test
   next (heavier — per-token minute-level wash series through the hold).
3. Detector module (`manipulation_detectors.py`) is the reusable core; calibrate to OUR
   zero-risk distribution (win ≈0.06 / loss ≈0.12 pre-entry), not the papers'.
4. Nothing touches live entry/exit without shadow evidence + Roy's sign-off.
