# Helius Entry-Timing Probe Findings — 2026-06-06

Ran `scripts/wallet_entry_timing_probe.py` on 7 (wallet, token) pairs to
classify recurring whales as accumulating / distributing / bag-holding.

## Headline verdicts

| Wallet | Token | First buy seen | Buys 7d | Sells 7d | Net pos | Verdict |
|---|---|---|---|---|---|---|
| 9ZPsRWG | PYTHIA | 2026-06-04 | 31 | 29 | **-576,946** | **DISTRIBUTING** (verdict label "SOLD_OUT") |
| 9ZPsRWG | arc | 2026-06-04 | 65 | 56 | **+297,698** | ACCUMULATING |
| 9ZPsRWG | ALCH | 2026-06-04 | 37 | 10 | **+14,893** | ACCUMULATING |
| 2Ejnns2 | ALCH | 2026-04-07 | 1 | 0 | -71,413 | Cycled out then re-entering (June +149k) |
| AgnaNYcN | ALCH | 2026-06-05 | 1 | 33 | -115,028 | Distributing hard |
| g848qLVX | ALCH | 2025-04-30 | 0 | 0 | -1,459,008 | Exited Oct 2025 |
| A9PzVmoB | ALCH | — | 0 | 0 | 0 | NO TRANSFERS FOUND (data discrepancy) |

## Key findings

### 1. 9ZPsRWG IS actively HFT-trading the operator-cluster tokens
- 60-120 swaps per token over 48 hours = ~1500 swaps/day total across portfolio
- Not the slow conviction-trader pattern; closer to scalping or market-making
- BUT they choose SPECIFIC TOKENS (operator-cluster only), so the signal-value is in their token CHOICE not their per-trade discretion
- The active-pinned promotion (2026-06-06 SQL) is still justified by cohort membership

### 2. PYTHIA is in DISTRIBUTION phase for 9ZPsRWG
- 753k out vs 176k in over 48h = net SHORT 576k
- Combined with $3.74M existing position (acquired before our 30-page window), this looks like 9ZPsRWG is exiting their PYTHIA bag
- **PYTHIA is NOT a pre-pump bet** — operator already taking profit

### 3. ALCH is the current ACCUMULATION token
- 37 buys vs 10 sells = net +14k
- AgnaNYcN shows opposite pattern (distributing hard, -115k)
- Mixed signals across the cohort — not a clean operator entry

### 4. The 30-page pagination cap is biting
- For highly active wallets (9ZPsRWG = ~1500 swaps/day), 3000 most-recent txs only spans ~2 days
- "First buy 2026-06-04" verdict is misleading — true first buy is likely months earlier
- Need either deeper pagination OR a different approach (e.g., direct tx lookup by mint via Birdeye/Solscan)

### 5. Data discrepancies
- A9PzVmoB: 0 transfers in Helius but appears in Solscan top-40 holders of ALCH
- g848qLVX: Helius shows them exited in Oct 2025, but Solscan shows current holding
- Likely causes: sub-account routing, Helius indexing gaps, or stale Solscan snapshots

## Strategic implications

- The "pre-emptive watchlist on PYTHIA" idea is killed — 9ZPsRWG is distributing PYTHIA
- ALCH is borderline — 9ZPsRWG accumulating but other cohort wallets exiting
- No clean pre-pump target identified from this probe
- The actionable signal remains: when 9ZPsRWG fires a NEW buy on a NEW token, our cluster detector catches it. We don't need to pre-emptively watchlist; we just need to be active and reactive.

## Recommendations

1. **Don't pre-emptively add PYTHIA/arc/ALCH to a token watchlist** — operator is mid-cycle on all three
2. **Run deeper probe with --max-pages 100 or 200** to find 9ZPsRWG's TRUE first-buy dates on these tokens
3. **Cross-check A9PzVmoB and g848qLVX via Solscan to resolve data discrepancy** — confirm whether they're real current holders
4. **Watch the shadow_signals table** for any new clusters that include 9ZPsRWG — that's the live signal we're now positioned to capture
