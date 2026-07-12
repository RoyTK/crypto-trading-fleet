# Telegram Channel Lead/Lag Findings — Info-Source Study, TG Arm

_2026-07-12. Sweep: 16 channels × 96 corpus runners via Telethon read-only search
(`research_tg_leadlag.py`); raw mentions in `tg_hits_2026-07-12.json`. Joined vs
intraday run-start (Birdeye 1H), Dexscreener promo paymentTimestamps, and earliest
genuine accumulator buys._

## Headline: public TG call channels are the SAME EVENT as the paid promo — not an earlier source

1. **Coverage:** 55/96 runners (57%) mentioned in ≥1 channel. Two channels dominate
   first-mentions: @CallAnalyserSol (aggregator) and @solana100xcall.
2. **★ The tell — near-total promo overlap:** **54/55 mentioned tokens ALSO had paid
   Dexscreener promotion**, and the TG call landed **median 0.9h AFTER the promo
   payment** (TG before promo in only 12/54 = 22%; many deltas within the same hour:
   −0.0h/−0.1h/−0.2h…). The call and the paid promo are one coordinated promotion
   package: pay Dexscreener → blast the channels, same hour.
3. **TG vs intraday run-start (n=24): 50/50, median +0.1h** — half the calls precede
   ignition, half chase it. Weaker lead than the promo payment itself (67-77%, ~13h).
   Often the call IS the ignition.
4. **TG vs earliest accumulator buy:** the naive "73% before" is a stale-mention
   artifact (aggregators mentioned old CAs in prior cycles, e.g. +16,000h leads on
   2024-era tokens). Restricting to run-adjacent tokens: **the smart wallets bought
   BEFORE the call in the large majority** (manlet −45.9h, PATTYICE −25.4h, Pigeon
   −91.5h, ok −114h, CASHCAT −154h, NORMIE/HAALAND/Joby minutes-before; only
   TBB +1.7h and Loom +9h went the other way).

## The fully measured information chain

**smart wallets accumulate → [paid promo + public TG call, same hour] → run ignition
(median ~13h later) → crowd.**

Public Telegram channels sit at the DISTRIBUTION layer of the promotion machine —
H2 confirmed for the public-channel tier. The "boards" where the real information
originates are upstream (private/paid rooms, insider dev↔KOL deals) — and our
wallet-copying already sits upstream of the public layer, which is exactly where
you want to be.

## Verdict per pre-registered gates (TG-as-entry-source)

- Precedes accumulators (run-adjacent): FAIL — wallets front-run the calls.
- Adds lead over the free promo feed: FAIL — median 0.9h BEHIND it, ~same-hour.
- → **Do NOT build a TG-call entry bot.** The machine-readable Dexscreener promo
  feed (already shadow-collected, Phase 2 live) captures this signal layer earlier,
  free, with zero scraping/ban risk. TG monitoring adds nothing for entry timing on
  this evidence (only 1/55 mentioned tokens lacked a paid promo = no independent
  channel-only alpha in the corpus).

## What survives

1. **Paid-promo signal** — the real tradeable layer (gate-4 passed; dud-rate
   verdict ~2026-08-01 from `promo_shadow_signals`).
2. **Insider-coupling signal (staged):** wallet accumulation followed within
   minutes-hours by a promo payment = insider-positioning signature (seen 3/8).
   Could become a cluster-style coincidence trigger later.
3. The Telethon session + collector are reusable for any future channel questions
   (e.g. vetting a specific caller, or if a channel emerges that calls WITHOUT promo).

## Caveats

- 16 public channels ≠ all of Telegram; private/paid rooms unmeasured (structurally
  can't be, without joining them).
- Telethon search matches exact CA text; ticker-only or image calls are missed
  (undercounts coverage, shouldn't bias timing much).
- n=24 on the intraday join; n≈10 clean run-adjacent accumulator comparisons.
