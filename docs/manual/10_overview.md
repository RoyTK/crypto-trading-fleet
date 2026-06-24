## Project Overview

_Last reviewed: 2026-06-24_

### What the project is

It is an **automated crypto-trading system** — a set of programs ("bots") that watch
cryptocurrency markets and trader wallets around the clock and decide when they *would*
buy and sell. Right now it trades on **paper** (pretend money) and records the results, so
Roy can see whether the strategies actually make money before risking real funds.

Think of it like a flight simulator for trading: the controls are real and the decisions
are real, but it is not yet flying a real plane.

### The two bots

There are two bots, each a different strategy:

- **STRUCTURE** — trades **Hyperliquid perpetual futures** (a derivatives exchange for
  large coins like Bitcoin and Ethereum). It looks for unusual market conditions (e.g.
  lots of forced selling) and bets on a bounce. **Currently PAUSED** — it kept losing
  paper money. A fix is believed possible but would need a ~$500/month paid data-plan
  upgrade, which isn't justified during the paper phase. So the project's active focus is
  COPY.

- **COPY** — trades **Solana "memecoins"** (tiny, very risky new tokens). It watches a
  curated list of skilled trader wallets; when several of them buy the same brand-new token
  within ~15 minutes, COPY buys too, then sells quickly to lock in gains before the token
  (often) collapses. *This is the more active bot and the one most of the day-to-day work
  is about.*

### Current status (keep this updated)

- **Mode:** Paper + a small "shadow" sample of real trades. **No meaningful real money is
  at risk.**
- **COPY** is the active focus: it trades on a **curated, vetted** list of ~150+ wallets
  that grows as Roy runs wallet discovery. Buying is currently **ON**.
- **STRUCTURE** is **PAUSED** (it was losing paper money; the likely fix needs a costly
  ~$500/mo data upgrade not worth paying for during the paper phase). It can be revisited
  later; for now, most of this manual's day-to-day is about COPY.
- The system is being **measured** against pass/fail criteria (the "kill criteria") over a
  ~60–90 day window before any decision to use real money.

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
