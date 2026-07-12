# Paid-Promo Lead/Lag Findings — Info-Source Study Phase 1 (non-TG arm)

_2026-07-11. Corpus: 98 runners (prerun_scans, May 19→Jul 9). Raw data:
`source_hits_2026-07-11.json`. Scripts: `research_source_leadlag.py` (collector),
`research_promo_intraday.py` (1H-candle classifier). Intraday results on server at
`/tmp/promo_intraday_results.json` (framework container)._

## Headline numbers

1. **65% of runners (64/98) had PAID Dexscreener promotion** (tokenProfile 70,
   communityTakeover 49, tokenAd 13, trendingBarAd 10 orders).
2. **Paid promo is strictly a pre-run behavior**: max lead was +1 day; across 98
   runners queried weeks later, essentially NOBODY pays to promote a token after it
   has already run. Promo ⇒ someone expects/intends a pump.
3. Day-granular leads split: 38 before run-day / 17 run-day / 9 day-after; plus a
   long stale tail (promos 84–638d old on revival tokens = prior-cycle noise).
4. **Intraday resolution of the decisive ±3d band (n=30 resolvable of 33):**
   - Rule A (scrape_runners' 12%-of-peak @1H): promo BEFORE run-start **23/30 (77%)**,
     median lead **+13.8h** (p25 +0.1h, p75 +35.1h).
   - Rule B (2x-median ignition): **20/30 (67%)**, median **+12.9h**.
   - Consistent across both definitions: **run-adjacent paid promo precedes ignition
     ~2/3–3/4 of the time with a ~13h median lead** — far beyond our ~7s–2min plumbing.
5. **BUT the smart wallets are earlier still (H2 signature at the wallet level):**
   promo preceded the earliest genuine accumulator buy in only **2/8** tokens where we
   hold accumulator timestamps. 6/8 times the accumulators bought FIRST — and in 3/8
   (manlet +0.5h, NORMIE +0.1h, Pigeon +0.1h) the accumulator bought **minutes-to-an-hour
   before the promo payment**: the signature of an insider positioning, then paying to
   promote their own bag.

## Interpretation (hold loosely — small n on the discriminator arm)

- **The information chain confirmed so far:** smart wallets (insiders/researchers) →
  paid promo → run ignition (~13h later median) → the crowd. Public "board" signals sit
  BETWEEN the wallets we already copy and the pump.
- **Roy's H1 is TRUE vs the market** (promo leads the run by hours = tradeable lead vs
  the crowd) **but H2-shaped vs our wallets** (they're upstream of the promo). Copying
  wallets remains the earlier signal — validates the current bot.
- Why promo may STILL be worth a strategy: our roster covers a tiny fraction of insider
  wallets. The promo feed is a **universal** signal (fires on tokens our 280 actives
  never touch). Entering at promo-time = after insiders, before the crowd — the
  profitable middle is plausible but UNPROVEN.
- Hybrid idea staged for later: "fresh accumulation + promo payment within hours" as a
  coincidence signal (cluster-style), given the 3/8 tight couplings.

## Caveats (pre-registered honesty)

- This measures **P(promo | run) = 65%**, NOT **P(run | promo)** — the trading question.
  Promos on tokens that then went nowhere are invisible in a runners-only corpus.
  → needs the live shadow collector (log ALL boosts/orders + forward returns incl. duds).
- n=8 on the accumulator-discriminator arm; 2/8=25% sits exactly on the pre-registered
  ≥25% gate — not decisive either way.
- 1H candles are coarse for the ±1h cases; stale-promo tail excluded by the ±3d band.
- /biz/ arm: DEAD (0/98 runners ever mentioned on warosu) — source eliminated.

## Pre-registered gate check (promo source)

| Gate | Threshold | Result | Verdict |
|---|---|---|---|
| Pre-run mention rate | ≥30% | 38-55/98 depending on staleness cut | ✅ pass |
| Median lead vs run-start | ≥30 min | ~13h | ✅ pass |
| Precedes earliest accumulator | ≥25% | 2/8 = 25% | ➖ borderline, n too small |
| Buy-at-mention sim net-positive | >0 | **NOT YET RUN** | ⏳ next step |

## Next steps

1. **Buy-at-promo forward sim** (gate 4): entry at first price ≥ promo ts, our exit
   stack (or first-pass: 72h MFE/MAE distribution on 1H candles), incl. slippage floor.
2. **TG arm** — blocked on the 5-min Telethon session step (Roy).
3. If gates hold → Phase 2 shadow collector: poll `token-boosts/latest/v1` + log every
   boosted token (signal AND the implicit control = boosted non-runners), track
   `fwd_mult_max` — answers P(run | promo) properly, slow_cluster pattern.
