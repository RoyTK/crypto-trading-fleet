## Project Overview

_Last reviewed: 2026-07-02_

### What the project is

It is an **automated crypto-trading system** — a set of programs ("bots") that watch
cryptocurrency markets and trader wallets around the clock and decide when they *would*
buy and sell. Right now it trades on **paper** (pretend money) and records the results, so
Roy can see whether the strategies actually make money before risking real funds.

Think of it like a flight simulator for trading: the controls are real and the decisions
are real, but it is not yet flying a real plane.

### The bots

The fleet is now **COPY-only**. There is one bot, **COPY**, running **three independent
strategies**. (STRUCTURE was decommissioned on 2026-06-25 — see below. SNIPER and EVENT
from the original plan were never built.)

- **COPY** — trades **Solana "memecoins"** (tiny, very risky new tokens). It watches a
  curated list of skilled trader wallets and buys when they do, then sells quickly to lock
  in gains before the token (often) collapses. COPY runs **three independent strategies**,
  each with its own bankroll, metrics, and emergency stop:
  - **Cluster** — buys when several tracked wallets buy the same brand-new token within
    ~15 minutes (the original COPY strategy).
  - **Conviction** — buys when a *single* highly-trusted wallet **deliberately accumulates**
    a position in one token (at least 3 buys spread over at least ~5 minutes — not a single
    snipe). It has its own **$10k paper bankroll** and separate metrics.
  - **Team-Follow** — an **experiment** (added 2026-07-01). It buys when **2 or more members
    of the same known "team"** of wallets co-buy the same token in a short window, drawn from
    a roster of 129 teams / 338 wallets. Own **$25k paper bankroll**; it's a forward test of
    the "many small losses, rare moonshots pay for it all" thesis and is net-negative so far
    on a small sample.

- **STRUCTURE** *(decommissioned 2026-06-25)* — formerly traded **Hyperliquid perpetual
  futures**, looking for unusual market conditions (e.g. lots of forced selling) and betting
  on a bounce. It kept losing paper money; the suspected fix needed a ~$500/month paid
  data-plan upgrade not worth paying for during the paper phase, so it was first parked and
  then fully removed (its idle container was spamming errors). The code is retained and it
  can be revived later if the data spend is ever justified.

### Current status (keep this updated)

- **Mode:** Paper + a small "shadow" sample of real trades. **No meaningful real money is
  at risk.**
- **COPY** is the whole fleet now: it trades on a **curated, vetted** list of wallets that
  grows as Roy runs wallet discovery, via three strategies (cluster + conviction +
  team-follow). Buying is currently **ON**.
- **STRUCTURE** is **DECOMMISSIONED** (removed 2026-06-25 — it was losing paper money and the
  likely fix needs a costly ~$500/mo data upgrade not worth paying for during the paper
  phase). Its code is retained and it can be revived later.
- The bottom-line measure is now simply **profitability (net PnL) per strategy** — shown on
  the daily digest and Grafana. (The old composite "promotion score," built for the original
  4-bot competition, has been retired.) The system is measured over a ~60–90 day window
  before any decision to use real money.

### The core philosophy (please respect these)

These principles are the whole point of the project. A maintainer should **not** undo them:

1. **Paper first.** Nothing trades real money until the data proves an edge AND Roy
   deliberately flips the live switches. Those switches are **off**.
2. **Human in the loop.** When the system thinks a bot is failing, it **alerts a human** —
   it does **not** automatically shut a bot down for strategy reasons. A person decides.
   (It *does* automatically stop on severe loss limits and emergencies.)
3. **Measure before tuning.** During the measurement window, the strategy settings are
   "locked." Changing them resets the measurement clock. Don't fiddle.
4. **Quality over quantity.** The wallet list COPY follows is carefully vetted; ~90% of
   candidate wallets are rejected. We add *good* wallets, not *many* wallets.

### How it stays alive without Roy

- It runs on a rented server that is always on.
- It **announces its health** in Discord (alerts) and on a Grafana dashboard.
- Most maintenance is: glance at the alerts, occasionally run a wallet-discovery pass, and
  respond if something turns red. The Operator track sections that follow walk through
  each of these.
