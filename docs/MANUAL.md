# Crypto Trading Fleet — Maintenance Manual

*Living document — rebuilt from `docs/manual/` by `scripts/build_manual.py`.*  
*Last built: 2026-06-24 18:28 UTC.*

> **How to read this:** **Part 1 — Operator track (sections 1.x)** is plain-language,
> for keeping the system alive day to day. **Part 2 — Engineer track (2.x)** is technical,
> for understanding and changing it. **Part 3 — Reference (3.x)** holds tools, decisions,
> troubleshooting, the access sheet, and the appendix. Use the table of contents
> or Ctrl-F to jump around.

---

## Table of Contents

  - [Summary](#summary)
  - [Who reads what](#who-reads-what)
  - [In an emergency](#in-an-emergency)
  - [What this is NOT](#what-this-is-not)
  - [Contacts & escalation](#contacts-escalation)
- **[Part 1 · Operator Track — Keeping It Alive (plain language)](#part-1-operator-track-keeping-it-alive-plain-language)**
  - [1.0 Project Overview](#10-project-overview)
    - [What the project is](#what-the-project-is)
    - [The two bots](#the-two-bots)
    - [Current status (keep this updated)](#current-status-keep-this-updated)
    - [The core philosophy (please respect these)](#the-core-philosophy-please-respect-these)
    - [How it stays alive without Roy](#how-it-stays-alive-without-roy)
  - [1.1 Operator Basics — Keeping It Alive](#11-operator-basics-keeping-it-alive)
    - [What's running, and where](#whats-running-and-where)
    - [How to tell it's healthy (your 2-minute daily check)](#how-to-tell-its-healthy-your-2-minute-daily-check)
    - [The alert levels — and what to do](#the-alert-levels-and-what-to-do)
    - [The emergency stop: `/panic`](#the-emergency-stop-panic)
    - [What NOT to touch](#what-not-to-touch)
    - [The rhythm of the job](#the-rhythm-of-the-job)
  - [1.2 Ask Claude — Your Built-In Expert](#12-ask-claude-your-built-in-expert)
    - [How to start Claude on this project (one-time-per-session)](#how-to-start-claude-on-this-project-one-time-per-session)
    - [What to ask it](#what-to-ask-it)
    - [Good habits](#good-habits)
  - [1.3 Recurring Tasks — Step by Step](#13-recurring-tasks-step-by-step)
    - [A. Run a wallet discovery + vetting pass (the main chore)](#a-run-a-wallet-discovery-vetting-pass-the-main-chore)
    - [B. Sync vetting results into the live system](#b-sync-vetting-results-into-the-live-system)
    - [C. Check Helius credit usage (watch the bill)](#c-check-helius-credit-usage-watch-the-bill)
    - [D. Responding to alerts](#d-responding-to-alerts)
    - [E. Quarterly whale-list refresh (STRUCTURE) — review only](#e-quarterly-whale-list-refresh-structure-review-only)
  - [1.4 Glossary (plain language)](#14-glossary-plain-language)
- **[Part 2 · Engineer Track — Understand & Continue (technical)](#part-2-engineer-track-understand-continue-technical)**
  - [2.0 Architecture & Codebase Map](#20-architecture-codebase-map)
    - [High-level shape](#high-level-shape)
    - [The bots](#the-bots)
    - [COPY data flow (webhook → trade → measurement)](#copy-data-flow-webhook-trade-measurement)
    - [The wallet pool lifecycle](#the-wallet-pool-lifecycle)
    - [The framework](#the-framework)
    - [Monitoring](#monitoring)
    - [Where to look for X](#where-to-look-for-x)
  - [2.1 Deployment & Infrastructure](#21-deployment-infrastructure)
    - [Topology](#topology)
    - [How code reaches the server (auto-pull deploy)](#how-code-reaches-the-server-auto-pull-deploy)
    - [Canonical manual deploy (when you must rebuild)](#canonical-manual-deploy-when-you-must-rebuild)
    - [Pausing auto-deploy (for debugging)](#pausing-auto-deploy-for-debugging)
    - [Migrations (Alembic)](#migrations-alembic)
    - [Fresh-server setup (rebuilding from nothing)](#fresh-server-setup-rebuilding-from-nothing)
    - [Local development](#local-development)
  - [2.2 Operations (Technical)](#22-operations-technical)
    - [Scheduled jobs](#scheduled-jobs)
    - [Key scripts (`scripts/`)](#key-scripts-scripts)
    - [Logs & state](#logs-state)
    - [Monitoring internals](#monitoring-internals)
    - [Wallet vetting — applying results manually](#wallet-vetting-applying-results-manually)
  - [2.3 Changing Safely](#23-changing-safely)
    - [The kill-criteria window lock](#the-kill-criteria-window-lock)
    - [Paper → shadow → live (the money switches)](#paper-shadow-live-the-money-switches)
    - [The trust/safety guardrails already in the code (don't remove)](#the-trustsafety-guardrails-already-in-the-code-dont-remove)
    - [Process for any change](#process-for-any-change)
    - [Going live with the manual edits to this doc](#going-live-with-the-manual-edits-to-this-doc)
  - [2.4 Disaster Recovery](#24-disaster-recovery)
    - [What can break, and how bad it is](#what-can-break-and-how-bad-it-is)
    - [Restore the database](#restore-the-database)
    - [Rebuild the whole server](#rebuild-the-whole-server)
    - [Rotate a leaked credential](#rotate-a-leaked-credential)
    - ["I don't know what's wrong" — safe default](#i-dont-know-whats-wrong-safe-default)
- **[Part 3 · Reference](#part-3-reference)**
  - [3.0 Tools & Resources](#30-tools-resources)
    - [Services](#services)
    - [Payments & crypto wallets (mostly for the eventual live phase)](#payments-crypto-wallets-mostly-for-the-eventual-live-phase)
    - [Reaching the Helius dashboard (the SOCKS tunnel)](#reaching-the-helius-dashboard-the-socks-tunnel)
    - [How we get vetted wallets (the discovery → trade pipeline)](#how-we-get-vetted-wallets-the-discovery-trade-pipeline)
  - [3.1 Why It Is The Way It Is](#31-why-it-is-the-way-it-is)
  - [3.2 Troubleshooting](#32-troubleshooting)
    - ["A discovery pass added 0 rows" / Discord shows ⚠ 0-rows](#a-discovery-pass-added-0-rows-discord-shows-0-rows)
    - ["claude is not recognized" (in a scheduled/script run)](#claude-is-not-recognized-in-a-scheduledscript-run)
    - ["Heartbeat silent" / a service went quiet (P1)](#heartbeat-silent-a-service-went-quiet-p1)
    - ["kill criterion fired" (P1)](#kill-criterion-fired-p1)
    - ["Helius credits spiking" / approaching the 30M cap](#helius-credits-spiking-approaching-the-30m-cap)
    - ["Helius webhook 400 / sync errors"](#helius-webhook-400-sync-errors)
    - ["A trade booked a fake profit on a rugged token"](#a-trade-booked-a-fake-profit-on-a-rugged-token)
    - ["I changed `.env` but nothing happened"](#i-changed-env-but-nothing-happened)
    - ["A deploy broke something"](#a-deploy-broke-something)
    - ["Helius dashboard won't load"](#helius-dashboard-wont-load)
    - [Known non-obvious gotchas (catalog)](#known-non-obvious-gotchas-catalog)
  - [3.3 Access Sheet (PRINT THIS — fill by hand)](#33-access-sheet-print-this-fill-by-hand)
    - [Accounts & consoles](#accounts-consoles)
    - [Crypto wallets & recovery phrases (the MOST sensitive items)](#crypto-wallets-recovery-phrases-the-most-sensitive-items)
    - [The live `.env` (the master key file)](#the-live-env-the-master-key-file)
    - [Who can use the emergency stop](#who-can-use-the-emergency-stop)
  - [3.4 Appendix](#34-appendix)
    - [Crontab inventory (server, UTC)](#crontab-inventory-server-utc)
    - [Env-var reference (names + purpose only — values live in `.env`)](#env-var-reference-names-purpose-only-values-live-in-env)
    - [Key files](#key-files)
    - [DB tables (quick map)](#db-tables-quick-map)
    - [Deeper docs (read these for detail this manual summarizes)](#deeper-docs-read-these-for-detail-this-manual-summarizes)
    - [Maintaining this manual](#maintaining-this-manual)


---

_Last reviewed: 2026-06-24_

## Summary

This project is a small **fleet of automated crypto-trading "bots"** that Roy built. It is
deliberately running in **paper mode** (simulated money) plus a tiny "shadow" sample of
real trades — it is **not** trading meaningful real money yet, by design. The system runs
on a rented Linux server ("Hetzner"), watches crypto markets and wallets, and records how
it *would* have traded so its edge can be measured before any real capital is risked.

This manual exists so that **someone other than Roy can keep it alive and continue it.**
It is written in two layers: a plain-language **Operator track** for keeping things running,
and a technical **Engineer track** for understanding and changing the code.

## Who reads what

| You are… | Start here | Goal |
|---|---|---|
| **Keeping it alive** (non-technical) | Operator track — sections **1.x** | Know it's healthy, do the recurring tasks, react to alerts, don't break anything |
| **Continuing development** (engineer) | Engineer track — sections **2.x** | Understand the architecture, deploy changes safely, set up a new server |
| **Looking something up** | Reference — sections **3.x** | Tools/costs/access, why decisions were made, troubleshooting, the access sheet |

You do **not** need to read this top to bottom. Use the Table of Contents (auto-generated
at the top of the built document) or Ctrl-F.

> **If in doubt, ask Claude.** The fastest way to understand or run anything here is to open
> VS Code, open Claude, and ask it in plain English — it can read this whole project. See
> **"Ask Claude — Your Built-In Expert."** You are never alone with this system.

## In an emergency

> **To stop ALL trading immediately:** in Discord, type **`/panic`** (Roy is the
> authorized user; another authorized user must be configured to use it). This halts every
> bot, cancels open orders, and closes positions. It is safe to use — the system is built
> to be stopped.
>
> **If something looks wrong but isn't urgent:** check the **#alerts Discord channel** and
> the **Grafana dashboard** first (see *Operator Basics*). Most problems announce
> themselves there. Nothing here trades real money without a deliberate switch being
> flipped, so when in doubt, **stop it and ask** — a halted bot loses nothing.

## What this is NOT

- It is **not** investment advice or a guaranteed money-maker. It is an experiment to
  measure whether a trading edge exists *before* risking real money.
- It does **not** trade your savings. Real-money trading is gated behind explicit flags
  that are currently **off** (see *Changing Safely*).

## Contacts & escalation

_Fill this in and keep it current:_

- **Owner:** Roy — _phone / email: _______________________
- **If Roy is unavailable, the technical contact is:** _______________________
- **Server provider:** Hetzner (account login on the Access Sheet)
- **Where the passwords live:** the printed **Access Sheet** (section 3.4) + your family
  password sheets.

---

# Part 1 · Operator Track — Keeping It Alive (plain language)

---

## 1.0 Project Overview

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

---

## 1.1 Operator Basics — Keeping It Alive

_Last reviewed: 2026-06-24_

This section is for the day-to-day "is it healthy, and what do I do if not" job. You do
**not** need to understand the code.

### What's running, and where

Everything runs on a **rented Linux server from Hetzner** (a company in Germany), inside
"containers" managed by a tool called **Docker**. You normally never touch the server —
it runs by itself. The pieces are: the two bots, a database (stores all the trades), a
dashboard (Grafana), and an alerter (sends Discord messages). The wallet-discovery work is
the one task done from **Roy's office Windows PC**, not the server.

### How to tell it's healthy (your 2-minute daily check)

1. **Discord #alerts channel** — this is the heartbeat. If it's quiet or only shows routine
   green/blue messages, things are fine. Red/urgent messages need attention (see *Alerts*
   below).
2. **The daily digest** — once a day a summary of COPY's last 24 hours is posted to Discord
   **and emailed to `trading@generalaisystems.com`**. Skim it (check that inbox if you
   don't watch Discord closely).
3. **Grafana dashboard** (a web page; login on the Access Sheet) — shows charts of each
   bot's health. You don't need to interpret every chart; just confirm it's loading and
   nothing is screaming red.

If all three look normal, you're done. That's the whole daily job most days.

### The alert levels — and what to do

Alerts come with a severity. **The level tells you how fast to react:**

| Level | How it reaches you | What it means | What to do |
|---|---|---|---|
| **P0** | **Discord ping** + Telegram | Emergency — trading halted, an emergency stop fired, or a big loss limit hit | **Look now.** Open Discord, read the message. If unsure, it's safe to leave the bots halted and call the technical contact. |
| **P1** | Discord ping (mention) | A bot was stopped, the system went quiet, or a "is this strategy failing?" warning fired | **Look within an hour.** Read the message. A "kill-criteria" warning is informational — it does **not** require you to do anything except be aware (a human decides later). |
| **P2** | Discord message (no ping) | Routine but worth noting — wallets added, a credit snapshot, a minor anomaly | Read when convenient. |
| **P3** | Folded into the daily digest | Pure information | No action. |

> **Note:** **SMS text alerts are NOT set up** (the Twilio service wasn't worth paying for
> during the paper phase). So even a P0 emergency reaches you via **Discord + Telegram**,
> not a text message. Keep Discord notifications on. If real-money trading is ever turned
> on, adding SMS is worth reconsidering.

When in doubt about any alert: **a stopped bot is safe**. Nothing bad happens by halting.

### The emergency stop: `/panic`

In Discord, typing **`/panic`** immediately halts every bot, cancels open orders, and
closes positions. Use it any time something feels wrong and you can't reach Roy. It is
designed to be used. (Only authorized Discord users can run it — make sure at least one
person besides Roy is set up; see the Engineer track if not.)

### What NOT to touch

These are the few ways a non-expert could actually cause harm. Avoid them unless an
engineer is guiding you:

- **Do not flip the "live money" switches.** There are settings named like
  `COPY_LIVE_ENABLED` / `COPY_LIVE_FULL_ENABLED` / `COPY_SOLANA_PRIVATE_KEY`. They are
  **off/empty** on purpose. Turning them on makes the bot trade **real money**.
- **Do not change the bots' strategy settings** (the `config.py` files) during the
  measurement window. It corrupts the experiment.
- **Do not delete the database** or the server.
- **Do not commit passwords** anywhere (more in the Access Sheet section).

When unsure, the safe move is always: **halt with `/panic`, then ask.**

### The rhythm of the job

- **Daily:** the 2-minute health check above.
- **Every few days:** run a **wallet discovery pass** to keep COPY's wallet list fresh
  (see *Recurring Tasks*). This is the main recurring chore.
- **Occasionally:** check Helius credit usage so the monthly bill stays in budget (see
  *Recurring Tasks*).
- **Quarterly:** a whale-list refresh for STRUCTURE runs automatically; you just review it.
- **On a red alert:** follow the table above.

---

## 1.2 Ask Claude — Your Built-In Expert

_Last reviewed: 2026-06-24_

**This is the single most useful skill for maintaining the project. If in doubt, ask
Claude.** Claude is an AI assistant that can read this entire project, explain anything in
plain language, run the routine tasks for you, and help diagnose problems. You do not need
to be technical — you just need to be able to open one program and type a question.

### How to start Claude on this project (one-time-per-session)

1. **Open VS Code.** It's the program with the blue folder-ribbon icon (search "VS Code" or
   "Visual Studio Code" in the Start menu).
2. **Make sure this project is open.** The title bar / the file list on the left should say
   **`CryptoTradingworkflow`**. If it doesn't:
   - In VS Code: **File → Open Folder…** → navigate to
     `C:\Projects\CryptoTradingworkflow` → **Select Folder**.
3. **Open Claude.** Click the **Claude icon** in the left-hand activity bar (or press
   **Ctrl+Shift+P**, type "Claude", and pick the option to open/start Claude). A chat panel
   opens on the side.
4. **Start typing.** That's it — you're talking to Claude, and it can see this whole
   project (code, this manual, and the notes).

### What to ask it

Talk to it like a knowledgeable colleague. For example:
- *"In plain English, what does the COPY bot do right now, and is it healthy?"*
- *"Walk me through running a wallet discovery pass step by step."*
- *"I got a P1 alert that says '… kill criterion fired'. What does that mean and do I need
  to do anything?"*
- *"Help me check the Helius credit usage and tell me if we're near the budget."*
- *"Something looks wrong on the dashboard — here's a screenshot. What is it telling me?"*
- *"Read the maintenance manual section on disaster recovery and summarize what I'd do if
  the server died."*

### Good habits

- **Be specific and paste what you see** — error messages, alert text, screenshots. Claude
  works best with the actual details in front of it.
- **Ask it to explain before acting.** *"Explain what this will do before you do it."*
- **It's safe to ask "is this safe?"** before any change. When something could affect real
  money or stop the bots, Claude will flag it (and so does this manual's *Changing Safely*
  section).
- **When truly stuck, the safe fallback is still `/panic`** in Discord (stops all trading,
  harms nothing), then ask Claude or call the technical contact.

> **Bottom line:** you are never alone with this project. Open VS Code, open Claude, and
> ask. Most questions in this manual can also just be asked directly — and Claude will
> point you to the right section or do the task with you.

---

## 1.3 Recurring Tasks — Step by Step

_Last reviewed: 2026-06-24_

These are the hands-on chores. Each is written as a recipe. They assume you can open a
terminal and Discord; the engineer sections explain the commands in more depth.

### A. Run a wallet discovery + vetting pass (the main chore)

**Why:** COPY only trades on a curated list of skilled wallets. Wallets go stale over time,
so we periodically find new good ones. This is done **attended** (you start it and let it
run) from Roy's office PC, because it drives a web browser.

**Important reality:** this is **not** a hands-off scheduled job. An attempt to run it fully
automatically (Windows Task Scheduler + headless Claude) does **not** work, because the
browser tool only exists in an *interactive* Claude session. So you run it yourself.

**Steps:**
1. On Roy's office PC, make sure **Chrome is open and logged in to Birdeye**
   (birdeye.so). It usually already is.
2. Open **Claude** (the VS Code extension, or an interactive `claude` session with the
   Chrome connection enabled via `/chrome`).
3. Give it the discovery prompt:
   `C:\Projects\CryptoTradingworkflow\bots\copy\discovery_automation\browser_discovery_vetting_prompt.txt`
   (open that file and paste its contents, or point Claude at it).
4. Let it run. It will browse Birdeye's trending tokens, look at the top traders, and
   **vet each wallet** — marking each KEEP / REJECT / TOO_FAST. It works in batches of ~20
   and saves as it goes, so it can be stopped and restarted without losing work.
5. Results are written to the OneDrive file:
   `C:\Users\Roy\OneDrive\Documents\Claude\vetted_watch_results.txt`.

**Then sync the results into the system — see task B.**

### B. Sync vetting results into the live system

The discovery pass writes to OneDrive. The live bot reads from the project's git repo. So
new results must be copied over and "applied."

The simplest reliable way (the way Roy and Claude have been doing it):
1. In a Claude session **in the project** (`C:\Projects\CryptoTradingworkflow`), ask it to
   "sync the OneDrive vetting file into the repo." It will compare
   `C:\Users\Roy\OneDrive\Documents\Claude\vetted_watch_results.txt` against the repo's
   `bots/copy/vetted_watch_results.txt`, append only the **new** rows (de-duplicated), and
   commit + push.
2. Pushing to git auto-deploys to the server within ~1 minute.
3. The server's daily job then **applies** the verdicts: KEEP wallets become **active**
   (the bot starts following them), REJECT wallets are removed, TOO_FAST are logged only.
   - To apply immediately instead of waiting for the daily job, an engineer runs on the
     server: `docker compose exec -T framework python -m scripts.apply_vetting_results --file /tmp/vw.txt`
     (after copying the file in — see the Engineer track).

You'll see a Discord summary when wallets are applied.

### C. Check Helius credit usage (watch the bill)

**Why:** the COPY bot uses a paid service called **Helius** to receive wallet activity.
Cost is driven almost entirely by how many wallets it watches and how active they are. The
budget cap is **30 million credits/month (~$149)**. You want to make sure adding wallets
isn't blowing the budget.

**Two ways:**

**Quick (no login) — how many credits we're burning, from our own data:** an engineer runs
on the server: `docker compose exec -T framework python -m scripts.credit_pool_snapshot`.
It posts a Discord summary (wallets, daily "deliveries" ≈ daily credit burn, and a
projection vs the 30M cap). This also runs automatically each day.

**Full (the dashboard) — the actual bill:** the Helius dashboard must be opened **through
the server** (the account was created from the server's German internet connection). The
method is an **SSH SOCKS tunnel**:
1. On your PC, open a terminal and run (keep it open while browsing):
   `ssh -D 9999 -N fleet hetzner.com`
   *(Use this exact form — `fleet` then a space then `hetzner.com`, port 9999. The
   "obvious" `fleet@hetzner.com` version does not work.)*
2. In **Firefox**, set a SOCKS proxy: Settings → search "proxy" → Manual proxy → SOCKS Host
   `127.0.0.1`, Port `9999`, choose **SOCKS v5**, check **"Proxy DNS when using SOCKS v5"**.
3. Visit `https://ifconfig.me` to confirm you're now coming from the German server IP, then
   open `https://dashboard.helius.dev` → log in (Access Sheet) → **Usage** shows credits
   used vs the cap.
4. When done, **turn the Firefox proxy back off** so normal browsing works.

If credits are trending toward the 30M cap, slow down how often you run discovery (task A),
or talk to the engineer about raising the budget.

### D. Responding to alerts

See the alert table in *Operator Basics*. In short:
- **P0 (text message):** look immediately; if unsure, leave bots halted and call the
  technical contact.
- **P1 (Discord ping):** look within the hour; a "kill-criteria" warning is just
  information.
- **P2/P3:** read when convenient.

### E. Quarterly whale-list refresh (STRUCTURE) — review only

Four times a year (Feb/May/Aug/Nov 1) the server automatically re-checks STRUCTURE's
"whale" wallet list and posts a Discord summary. **You don't have to run anything** — just
read the summary. If it flags that many whales have gone bad, tell the engineer; updating
the list is a small code change they make.

---

## 1.4 Glossary (plain language)

_Last reviewed: 2026-06-24_

Terms you'll meet in this manual, the dashboards, and Discord — in everyday language.

- **Bot** — a program that trades automatically by following fixed rules. We have two:
  STRUCTURE and COPY.
- **Paper trading** — pretend trading. The bot records what it *would* have done, with no
  real money. This is the current mode.
- **Shadow trading** — a small fraction (~10%) of paper trades are also done for real with
  tiny amounts, just to check the simulation is realistic. Still not meaningful money.
- **Live trading** — real money. Currently **off**, gated behind explicit switches.
- **STRUCTURE** — the bot trading Hyperliquid perpetual futures (big coins, derivatives).
- **COPY** — the bot trading Solana memecoins by copying skilled wallets.
- **Wallet** — a crypto account, identified by a long string of letters/numbers (an
  "address"). COPY watches a curated list of *skilled* wallets.
- **Cluster** — the core COPY signal: when **several tracked wallets buy the same new token
  within ~15 minutes**, that "cluster" of buys is the cue for COPY to buy too.
- **Vetting** — checking whether a candidate wallet is actually good to follow. Each gets a
  verdict:
  - **KEEP** — good; the bot should follow it (becomes "active").
  - **REJECT** — bad (a bag-holder, a bot, junk); discard.
  - **TOO_FAST** — genuinely skilled but trades *so* fast (seconds) that COPY can't keep
    up; logged for a possible future faster bot, not used now.
- **Active / Watch / Pruned** — the three "tiers" a wallet can be in:
  - **Active** — followed; its buys can trigger trades.
  - **Watch** — observed but not driving trades (mostly empty now; vetted wallets go
    straight to active).
  - **Pruned** — dropped; no longer watched.
- **Kill criteria** — the pass/fail scorecard for each bot over the measurement window
  (win rate, profit, risk-adjusted "Sharpe" number). If a bot trips a criterion, the system
  **warns a human**; it does not auto-shut-down for this.
- **Sharpe** — a number measuring profit *relative to* how bumpy the ride was. Higher is
  better; it's the headline "is this strategy actually good" metric.
- **Webhook** — a way for the Helius service to instantly notify our system whenever a
  watched wallet trades. Each notification ("delivery") costs a small credit.
- **Credits** — the unit Helius bills in. Our budget is 30 million/month (~$149). Watching
  more/active wallets uses more credits.
- **Rug / rug-pull** — when a memecoin's creators pull out all the money and the token
  collapses to zero. Common; COPY's whole edge is selling *before* this happens.
- **Hetzner** — the company we rent the always-on server from (located in Germany).
- **Docker / container** — the technology that runs each part of the system in its own
  tidy box on the server.
- **Grafana** — the dashboard web page showing health charts.
- **Discord / Telegram** — chat apps the system posts alerts to. `/panic` is typed here.
- **`/panic`** — the emergency stop command (halts all bots).
- **Git / push / deploy** — git is where the code lives; "pushing" a change automatically
  updates ("deploys") the server within about a minute.
- **`.env`** — a private settings file on the server holding all the passwords/keys. Never
  shared or committed to git.

---

# Part 2 · Engineer Track — Understand & Continue (technical)

---

## 2.0 Architecture & Codebase Map

_Last reviewed: 2026-06-24_

For an engineer who has never seen the repo. Deeper, frozen design rationale lives in
`design_state_2026-04-26.md` (a snapshot — treat as history, not current truth).

### High-level shape

A shared **framework** + two **bots**, all Python, all running as Docker containers on one
Hetzner host, backed by **PostgreSQL** (state) and **Redis** (pubsub/dedup). Code reaches
the server by `git push` (auto-pull deploy). Monitoring is **Grafana/Prometheus** +
multi-channel alerting (Discord/Telegram/Twilio).

```
                 ┌────────── framework/ (shared) ──────────┐
                 │ DB models, kill-criteria, alerts, audit, │
                 │ heartbeat/watchdog, reconciliation,      │
                 │ scoring, reporting                       │
                 └──────────────────────────────────────────┘
   bots/structure/  (Hyperliquid perps)     bots/copy/  (Solana memecoins)
            │                                       │
        Postgres  ◄───────── shared ───────────►  Redis
            │                                       │
   monitoring/ (webhook_receiver, alerting dispatcher, dashboards, prometheus)
```

### The bots

**STRUCTURE** (`bots/structure/`) — Hyperliquid perpetual futures. Signals in
`bots/structure/signals/`: `funding_fade`, `liquidation_cascade`, `whale_flip`,
`hl_oi_divergence`. Reads market data via the Hyperliquid Info API (`venue.py`). Paper +
~10% shadow. **Currently PAUSED** — it was losing paper money; the suspected fix needs a
~$500/mo real-time data-feed upgrade (e.g. the Coinglass liquidation tier feeding
`liquidation_cascade`), not justified in the paper phase. Code is intact; revisit later.

**COPY** (`bots/copy/`) — Solana memecoins via wallet-cluster copying. This is the active
focus. Key files:
- `main.py` — the loop: subscribes to wallet events, runs cluster detection, manages
  positions.
- `signals/cluster.py` — the buy signal (≥3 distinct active wallets buy the same token
  within a 15-min rolling window, ≥$1k each).
- `signals/sell_cluster.py` — the defensive exit (≥2 tracked wallets selling).
- `trailing_stop.py` — tiered partial-exit ladder + price-scale anomaly guard.
- `wallet_pool_manager.py` — **pure logic** for active/watch/pruned tier decisions.
- `venue/` — external integrations: `helius_webhooks.py`, `helius_solana.py`, `birdeye.py`,
  `dex_quoter.py`, `jupiter_swap.py`, `cielo.py`.
- `config.py` — locked thresholds (see *Changing Safely*). **Current state: cluster buys
  ENABLED; `copy_active_list_target = 300`; KEEP wallets go directly to `active`.**

### COPY data flow (webhook → trade → measurement)

1. A tracked wallet trades on Solana → **Helius** sends a webhook to
   `monitoring/webhook_receiver/main.py` (one endpoint for `active`, one for `watch`).
2. The receiver validates the auth header, logs a row to `wallet_events_log`
   (`source_webhook` = active|watch), and for **active** wallets publishes a buy/sell event
   to Redis (`copy:buys` / `copy:sells`).
3. `bots/copy/main.py` subscribes, feeds buys to `signals/cluster.py` (in-memory 15-min
   rolling window per token).
4. When ≥3 distinct active wallets cluster on a token, a signal fires → de-duped via the
   `cluster_detections` table → a paper trade is opened (`executor.py` → `trades` row),
   optionally a ~10% **shadow** swap via Jupiter.
5. Positions are managed every ~60s: price via Birdeye/Jupiter, tiered partial exits,
   stops, **rug check** (Birdeye liquidity < floor → book ~total loss), sell-cluster exit.
6. On close, **PnL is attributed** equally to the wallets in the cluster
   (`wallet_attributions`) — this feeds wallet promotion/demotion and the leaderboard.
7. **kill-criteria** (`framework/kill_criteria_monitor.py`) reads closed trades and scores
   the bot (N, win rate, net PnL, Sharpe) against its window.

### The wallet pool lifecycle

- New wallets enter **already vetted** (via browser discovery) and go **straight to
  `active`** (KEEP), applied by `scripts/apply_vetting_results.py`.
- The daily job `scripts/wallet_pool_daily_cron.py` recomputes activity, **demotes** proven
  losers (by attributed PnL, not raw activity), **prunes** persistent losers, and **syncs**
  the active+watch address lists to Helius (`venue/helius_webhooks.py::sync_pool_tiers`).
  Both active and watch are webhook-subscribed (so both cost Helius credits).
- `wallet_pool_manager.decide_tier_changes()` is the pure decision function (well tested in
  `tests/test_wallet_pool_manager.py`). Promotion is **vetted-only**
  (`copy_promote_vetted_only`).

### The framework

- `framework/models.py` — all DB tables (see Appendix for the table list). Key ones:
  `trades`, `signals`, `wallet_pool`, `wallet_events_log`, `wallet_attributions`,
  `cluster_detections`, `bot_state` (holds `kill_criteria_status` JSON), `halts`, `scores`,
  `audit_log`, `heartbeats`.
- `kill_criteria_monitor.py` — window dates + scoring; **alerts only, no auto-halt** for
  strategy criteria.
- `dd_monitor.py` / `halt_state.py` — drawdown limits that **do** auto-halt; `/panic`.
- `alerts.py` (`emit_alert`) → Redis `alerts:emit` → `monitoring/alerting/dispatcher.py` →
  Discord/Telegram/Twilio connectors.
- `audit.py` (`write_audit`) → append-only `audit_log`.
- `heartbeat.py` / `watchdog.py` — every process pings every 30s; watchdog escalates
  staleness.
- `reconciliation.py` — periodic paper-vs-venue consistency check.
- `scoring/` — promotion score; `reporting/` — daily report + digest.

### Monitoring

- `monitoring/webhook_receiver/` — the public HTTPS endpoint Helius calls.
- `monitoring/alerting/` — taxonomy (P0–P3), dispatcher, connectors.
- `monitoring/dashboards/*.json` — Grafana (fleet-overview, structure-detail, copy-detail,
  cluster diagnostics); auto-reloaded, no restart needed.
- `monitoring/prometheus/` — scrape config.

### Where to look for X

| Want to… | Look in |
|---|---|
| Change a COPY threshold | `bots/copy/config.py` (locked — read *Changing Safely* first) |
| Understand the buy signal | `bots/copy/signals/cluster.py` |
| Change wallet promote/demote logic | `bots/copy/wallet_pool_manager.py` + `tests/test_wallet_pool_manager.py` |
| Add/inspect a DB table | `framework/models.py` + `framework/alembic/versions/` |
| Change alert routing | `monitoring/alerting/` |
| Add a scheduled job | `scripts/` + the crontab (see *Operations (technical)*) |
| The wallet discovery prompt/automation | `bots/copy/discovery_automation/` |

---

## 2.1 Deployment & Infrastructure

_Last reviewed: 2026-06-24_

### Topology

One Hetzner VPS runs everything via `docker-compose.yml`: `postgres`, `redis`,
`prometheus`, `grafana`, `framework`, `scoring`, `alerting`, `report_cron`,
`bot_structure`, `bot_copy`, `bot_copy_webhook_receiver`, and a one-shot `migrate`
(profile `tools`). A single `framework/Dockerfile` builds the Python image used by all
app services.

### How code reaches the server (auto-pull deploy)

A cron on the server runs `scripts/hetzner_autopull.sh` **every minute**. It:
1. `git fetch origin main`; if `origin/main` is ahead, `git reset --hard origin/main`.
2. Detects changed files and restarts only affected services (mapping below).
3. Runs `alembic upgrade head` if new migration files appeared.

So the normal deploy is just: **commit and push to `main`** → live within ~60s.

> **Consequence — untracked files get wiped.** `reset --hard` discards anything not
> committed. Files the bot needs must be **committed**, not `scp`-ed in. (This is why the
> vetting results live in the repo.)

**Changed-file → restart mapping (approximate):**
| Changed path | Restarts |
|---|---|
| `bots/copy/**` | `bot_copy` + `bot_copy_webhook_receiver` |
| `bots/structure/**` | `bot_structure` |
| `framework/scoring/`, `kill_criteria_monitor.py`, etc. | `scoring` |
| `framework/alembic/versions/**` | run migrations, then `scoring` |
| shared framework (`models.py`, `db.py`, `alerts.py`, …) | all services |
| `monitoring/alerting/**` | `alerting` |
| `monitoring/dashboards/**` | nothing (Grafana auto-reloads) |
| `scripts/**`, `tests/**`, `*.md`, `docs/**`, `memory/**` | nothing |

### Canonical manual deploy (when you must rebuild)

Auto-pull does NOT handle changes to `docker-compose.yml`, the `Dockerfile`, or
`requirements.txt`, and it does NOT re-read `.env`. For those, on the server:

```bash
cd ~/crypto-fleet && git pull origin main && \
  docker compose build <services...> && \
  docker compose up -d --force-recreate <services...>
```

Common full form used this project:
```bash
cd ~/crypto-fleet && git pull origin main && \
  docker compose build framework bot_copy scoring && \
  docker compose up -d --force-recreate framework bot_copy scoring
```

> **`.env` gotcha:** `docker compose restart` does **not** re-read `.env`. After editing
> `.env` you must `docker compose up -d --force-recreate <service>` and verify with
> `docker compose exec <service> printenv VAR`.

### Pausing auto-deploy (for debugging)

```bash
touch ~/crypto-fleet/.autopull_paused   # pause
rm    ~/crypto-fleet/.autopull_paused   # resume
tail -f ~/autopull.log                  # watch deploys
```

### Migrations (Alembic)

Schema is versioned in `framework/alembic/versions/`. Auto-pull runs `alembic upgrade head`
when a new migration lands. Manually: `docker compose run --rm migrate`. To add one, write
a revision, test locally, commit — it deploys and applies automatically.

### Fresh-server setup (rebuilding from nothing)

If you must stand up a new host:
1. Provision a Hetzner VPS (CPX32-class), add your SSH key, harden SSH.
2. Install Docker + Docker Compose plugin and `git`.
3. `git clone` the repo to `~/crypto-fleet`.
4. Create `~/crypto-fleet/.env` from `.env.example` and fill **every** secret (from the
   Access Sheet / your offline `.env` printout). This is the critical, manual step.
5. `docker compose build && docker compose up -d` (postgres/redis first via healthchecks).
6. `docker compose run --rm migrate` to create the schema.
7. Re-create the Helius webhooks: `docker compose exec framework python -m scripts.helius_webhook_setup`.
8. Re-install the server crontab (see *Operations (technical)* for the lines), including
   `hetzner_autopull.sh` every minute.
9. Re-establish remote access (Cloudflare Tunnel — see `monitoring/cloudflared/README.md`).
10. Confirm Discord/Grafana/heartbeats are green.

### Local development

- `cp .env.example .env` and fill at least the DB/Redis vars (external API keys optional
  for unit tests).
- `docker compose up -d postgres redis` for a local DB, or point at a throwaway DB.
- Run tests: `python -m pytest` (e.g. `tests/test_wallet_pool_manager.py`).
- Most logic (tier decisions, signals) is unit-testable without live services — prefer
  that over hitting real APIs.

---

## 2.2 Operations (Technical)

_Last reviewed: 2026-06-24_

### Scheduled jobs

Two layers: **APScheduler** inside the `scoring`/`report_cron` containers (drawdown checks
every 5 min, kill-criteria every 60 min, scoring, daily report ~07:00 local), and the
**server OS crontab** for `docker compose exec` scripts. Verify the live crontab with
`crontab -l`. As of this writing it contains (Hetzner, UTC):

| Schedule | Job | Notes |
|---|---|---|
| `* * * * *` | `scripts/hetzner_autopull.sh` | auto-deploy |
| `0 7 * * *` | `scripts.wallet_pool_daily_cron` | recompute activity, demote/prune, **sync Helius** |
| `30 7 * * *` | `scripts.apply_vetting_results` | ingest vetting verdicts (after `docker compose cp` of the file) |
| `15 13 * * *` | `scripts.credit_pool_snapshot` | daily Helius credit/pool snapshot → audit_log + Discord |
| `~13:00` | `scripts.daily_digest` | COPY 24h summary (verify it's installed) |
| every 5h | whale_pool_growth (STRUCTURE) | |
| weekly | whale_graduation_scan (STRUCTURE) | |
| `0 14 1 2,5,8,11 *` | `scripts.quarterly_whale_refresh` | quarterly whale review |
| `0 6/14/22 * * *` | 3× `scripts.wallet_pool_discovery` | **PAUSED** (commented out — 0% keepers; replaced by browser discovery) |

To re-pause/resume a cron line, edit non-interactively, e.g.:
`(crontab -l; echo '<line>') | crontab -` — never paste a cron line straight into the shell.

### Key scripts (`scripts/`)

| Script | Purpose |
|---|---|
| `hetzner_autopull.sh` | the per-minute deploy loop |
| `wallet_pool_daily_cron.py` | daily pool reconcile + Helius sync |
| `apply_vetting_results.py` | KEEP→active, REJECT→pruned, TOO_FAST→logged; idempotent; `--dry-run` |
| `credit_pool_snapshot.py` | credits-vs-wallets curve into audit_log (proxy = `wallet_events_log` count) |
| `helius_webhook_setup.py` | create/sync/`--list`/`--delete` Helius webhooks |
| `daily_digest.py` | COPY daily Discord summary |
| `quarterly_whale_refresh.py` | STRUCTURE whale re-check (review-only output) |
| `correct_rug_trade.py` | fix a fictitious post-rug paper exit (targeted, audited) |
| `build_manual.py` | **this manual's builder** |

Run any with: `docker compose exec -T framework python -m scripts.<name> [args]`.

### Logs & state

```bash
docker compose ps                                   # what's running
docker compose logs -f --tail=50 bot_copy           # follow a service
docker compose exec -T postgres psql -U fleet -d fleet -c "<SQL>"   # query DB
tail -f ~/autopull.log                              # deploys
```

Useful SQL:
```sql
-- bot state + kill-criteria snapshot
SELECT bot_id, state, halted_until, halt_reason,
       kill_criteria_status->>'n' AS n,
       kill_criteria_status->>'wr' AS wr,
       kill_criteria_status->>'sharpe' AS sharpe
FROM bot_state;

-- wallet pool composition
SELECT tier, COUNT(*) FROM wallet_pool WHERE chain='solana' GROUP BY tier;

-- recent COPY trades
SELECT entry_at, asset, size_usd, exit_reason, pnl_usd
FROM trades WHERE bot_id='copy' ORDER BY entry_at DESC LIMIT 20;

-- is it alive?
SELECT process_name, last_ping_at FROM heartbeats ORDER BY last_ping_at DESC;
```

### Monitoring internals

- **Alert pipeline:** any code calls `framework.alerts.emit_alert(severity, title, body,…)`
  → Redis `alerts:emit` → `monitoring/alerting/dispatcher.py` → connectors
  (`discord_connector`, `telegram_connector`, `twilio_connector`). Severity → channel
  routing is in `monitoring/alerting/taxonomy.py`.
- **Heartbeats:** each process writes `heartbeats(process_name, last_ping_at)` every 30s;
  `framework/watchdog.py` escalates staleness (P1 → restart attempt → P0).
- **Grafana:** dashboards in `monitoring/dashboards/*.json`, auto-reloaded. The
  kill-criteria status is on the per-bot detail dashboards.
- **Health truth for COPY discovery:** "did a discovery pass produce rows" is the real
  signal (a dead browser exits cleanly with 0 rows) — that's why discovery reports a
  row-count to Discord.

### Wallet vetting — applying results manually

```bash
cd ~/crypto-fleet && git pull origin main && \
  docker compose cp bots/copy/vetted_watch_results.txt framework:/tmp/vw.txt && \
  docker compose exec -T framework python -m scripts.apply_vetting_results --file /tmp/vw.txt
```
(`docker compose cp` is required because the container runs **baked** code and can't see a
host file otherwise.)

---

## 2.3 Changing Safely

_Last reviewed: 2026-06-24_

The system is a **measurement experiment**. Careless changes either corrupt the
measurement or risk real money. These are the guardrails.

### The kill-criteria window lock

Each bot is being scored over a fixed window (see `framework/kill_criteria_monitor.py` and
the `config.py` headers). **The strategy constants in `bots/*/config.py` are LOCKED during
the window.**

- Changing a locked threshold **resets the measurement clock** — unless the change is a
  legitimate data-driven correction that meets ALL of: (1) justification written to
  `audit_log` *before* the change, (2) it names the observation forcing it, (3) it
  *shrinks* behavior space (more conservative, not more permissive), (4) at most once per
  parameter per window.
- Changes that do **not** reset the window: bug fixes, logging/dashboards/monitoring,
  docs, and the wallet pool churning through its normal tier rules.
- **Materially changing the wallet pool** (e.g., raising `copy_active_list_target`) shifts
  which signals fire — note it in `audit_log` as a pool-expansion marker so before/after
  data isn't conflated.

If you're unsure whether a change resets the window: assume it does, and ask.

### Paper → shadow → live (the money switches)

Real money is gated behind explicit flags, currently **off**:
- `COPY_LIVE_ENABLED` — enables the real-swap signing path (shadow).
- `COPY_LIVE_FULL_ENABLED` — full-size real swaps.
- `COPY_SOLANA_PRIVATE_KEY` — the funded wallet's secret (empty now).
- STRUCTURE: `HYPERLIQUID_AGENT_PRIVATE_KEY` (trade-only agent key).

**Do not flip these casually.** The go-live procedure (only after the data justifies it and
Roy decides) is roughly: generate/fund a wallet → set the key in `.env` →
`COPY_LIVE_ENABLED=true` with `COPY_LIVE_FULL_ENABLED=false` (shadow only) → watch
calibration → only then `COPY_LIVE_FULL_ENABLED=true`. Each step is deliberate and
monitored.

### The trust/safety guardrails already in the code (don't remove)

- **Rug check before closing a paper trade** — if Birdeye liquidity has collapsed, the bot
  books a ~total loss instead of a fictitious profit at a stale price.
- **Price-scale anomaly guard** (`trailing_stop.py`) — skips a management cycle if the
  price ratio is absurd (>10,000×), catching oracle/decimals glitches.
- **Slippage floor** — paper fills assume a minimum realistic slippage (150 bps) so
  results aren't rosy.
- **Vetting-before-ingest** — wallets are validated (base58, vetted source) before they can
  drive trades; discovery output is bounded per run.
- **kill-criteria = alerts only** — strategy failure warns a human; it does not auto-trade
  or auto-promote.

### Process for any change

1. Make it on a branch or commit to `main` only when ready (push = deploy in ~60s).
2. Run the relevant tests (`python -m pytest tests/...`). The wallet-pool logic is well
   covered — keep it that way.
3. If it touches locked config, write the `audit_log` justification **first**.
4. After deploy, verify: `docker compose ps`, logs, the relevant Discord/Grafana signal.
5. Never skip git hooks or commit secrets.

### Going live with the manual edits to this doc

Remember the Access Sheet warning: when COPY eventually trades real money, the wallet
discovery → auto-commit-to-`main` pipeline should gain a **human review gate** (commit to a
branch, merge after a glance) instead of auto-pushing — untrusted page content feeding a
real-money bot needs a person in the loop.

---

## 2.4 Disaster Recovery

_Last reviewed: 2026-06-24_

### What can break, and how bad it is

| Scenario | Severity | Recoverable? |
|---|---|---|
| A container crashed / went silent | Low | Yes — watchdog tries to restart; else `docker compose up -d <svc>` |
| Bad deploy | Low | Yes — revert the commit and push; auto-pull rolls forward |
| Database lost | High | Yes if backups exist — restore (below). Trade history is the main loss |
| Whole server lost | High | Yes — rebuild a fresh server (see *Deployment → Fresh-server setup*) + restore DB |
| `.env` / secrets lost | **Critical** | Only if you have the offline copy — **this is the one thing git cannot recover** |

The single most important disaster-prep fact: **secrets are not in git.** If the server and
your offline `.env`/Access Sheet are both gone, the API keys and wallet keys are gone. Keep
the printed Access Sheet and a printed/encrypted `.env` somewhere safe and separate.

### Restore the database

Hetzner provides automated daily VPS snapshots/backups (enable in the Hetzner console; ~$4/mo).
To recover:
1. Restore the VPS from the most recent Hetzner snapshot **or** stand up a fresh server and
   restore just Postgres from a `pg_dump` if you keep one.
2. If using a logical dump: `docker compose up -d postgres`, then
   `cat backup.sql | docker compose exec -T postgres psql -U fleet -d fleet` (include an
   explicit `COMMIT;` in SQL files — session-EOF behavior is unreliable here).
3. Bring up the rest: `docker compose up -d`.
4. Re-sync Helius webhooks: `docker compose exec framework python -m scripts.helius_webhook_setup`.
5. Verify: `docker compose exec postgres psql -U fleet -d fleet -c "SELECT count(*) FROM trades;"`
   and confirm heartbeats are fresh.

> Recommended hygiene: a monthly "restore to scratch" test so you *know* the backup works.

### Rebuild the whole server

Follow **Deployment → Fresh-server setup**. The only inputs you cannot regenerate are the
**secrets** (`.env`) — everything else (code, schema, dashboards) is in git. Budget a few
hours; the gating step is filling `.env` correctly from the Access Sheet.

### Rotate a leaked credential

If an API key or webhook secret leaks: rotate it in that service's console (Access Sheet
has the URLs), update `.env` on the server, `docker compose up -d --force-recreate <svc>`,
and for Helius re-run `helius_webhook_setup`. Never put the new secret in git.

### "I don't know what's wrong" — safe default

1. `/panic` in Discord (halts all trading; loses nothing — it's paper).
2. `docker compose ps` and `docker compose logs --tail=100 <noisy service>`.
3. If a deploy caused it, `git revert` the last commit and push.
4. Call the technical contact. A halted, paper-only system is in no danger sitting still.

---

# Part 3 · Reference

---

## 3.0 Tools & Resources

_Last reviewed: 2026-06-24_

Every external service the project depends on — what it's for, what it costs, where its
console is, how to get in, and the `.env` variable name(s). **Actual passwords/keys are on
the Access Sheet, never here.**

### Services

| Service | What it's for | ~Cost/mo | Console | Access notes | Env var(s) |
|---|---|---|---|---|---|
| **Hetzner** | The always-on server (and backups) | ~$25 + $4 backup | console.hetzner.cloud | Account login; SSH key for the box | (SSH key, not in `.env`) |
| **GitHub** | Source code; `git push` = deploy | Free | github.com/RoyTK/crypto-trading-fleet | SSH key with push to `main` | (SSH key) |
| **Helius** | Solana wallet webhooks (COPY's lifeblood) | **$49** Developer (10M credits) + autoscale $5/1M, **$149 cap = 30M** | dashboard.helius.dev | **Reach via the SSH SOCKS tunnel** (account is German-IP bound) — see below | `HELIUS_API_KEY`, `HELIUS_WEBHOOK_AUTH_SECRET`, `HELIUS_RPC_URL` |
| **Cielo** | Wallet PnL/win-rate stats (curation) | $65 Pro | app.cielo.finance | Account login | `CIELO_API_KEY` |
| **Birdeye** | Token prices, liquidity, trader discovery | Free/Lite (~$0–19) | birdeye.so | Logged-in Chrome session (used by discovery) + API key | `BIRDEYE_API_KEY` |
| **Jupiter** | Solana DEX quotes/swaps | Free | jup.ag | Public API, no key | (none) |
| **Coinglass** | Liquidation data (STRUCTURE) | $0 (disabled) | coinglass.com | Disabled via `STRUCTURE_LIQ_CASCADE_ENABLED=false`; its **real-time tier (~$500/mo)** is the upgrade STRUCTURE would need to be revived — not justified in paper phase | `COINGLASS_API_KEY` |
| **Discord** | Alerts + `/panic` + `/status` | Free | discord.com | Bot in the server; owner ID authorizes `/panic` | `DISCORD_BOT_TOKEN`, `DISCORD_*_CHANNEL_ID`, `DISCORD_OWNER_USER_ID`, `COPY_DISCORD_WEBHOOK` |
| **Telegram** | Alerts + `/panic` (backup channel) | Free | t.me | Bot via BotFather; owner ID | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_USER_ID` |
| **Twilio** | P0 emergency SMS | **$0 — NOT set up** | console.twilio.com | Not configured during paper phase (SMS wasn't worth paying for); P0 reaches you via Discord + Telegram instead. Add if going live. | `TWILIO_*` (unset) |
| **Privacy.com** | Virtual cards used to **pay for some services** (e.g. Helius) | per-card | privacy.com | Roy's account; login on the Access Sheet | (none) |
| **Email (SMTP)** | Daily digest/report email | included | (provider) | Digest is sent to **`trading@generalaisystems.com`** | `SMTP_*`, `SMTP_TO` |
| **OneDrive** | Where browser discovery writes vetting results | Included (M365) | onedrive.com | Roy's account; files at `C:\Users\Roy\OneDrive\Documents\Claude\` | (none) |
| **Claude Max** | Runs the browser wallet-discovery/vetting | Included (Max sub) | claude.ai | Max subscription; the discovery uses Max quota, not an API key | (uses Max login, not `.env`) |
| **Cloudflare** | Secure remote access to the server | Free | one.dash.cloudflare.com | Tunnel token | `CLOUDFLARE_TUNNEL_TOKEN` |

Rough monthly total today: **~$140–150** (Hetzner + Helius + Cielo + Birdeye; **no SMS**).
The Helius bill is the variable one — it scales with how many/active wallets COPY watches.
Some services are paid via a **Privacy.com** virtual card rather than a card directly.

### Payments & crypto wallets (mostly for the eventual live phase)

- **Privacy.com** — virtual cards used to pay for some subscriptions (e.g. Helius). Login
  on the Access Sheet.
- **Crypto wallets / exchange** — used to hold and fund trading capital (relevant once live
  trading is on; mostly idle now): **Phantom** (Solana), **Rabby** and **MetaMask** (EVM
  chains), and **Kraken** (exchange / on-ramp). Each browser wallet has a **12-word
  recovery phrase** — the most sensitive secrets in the whole project. They live on the
  Access Sheet with extra-secure handling; **never** type or store them anywhere digital
  connected to this project.

### Reaching the Helius dashboard (the SOCKS tunnel)

The Helius account is tied to the server's German IP, so the dashboard must be opened
*through* the server:
1. `ssh -D 9999 -N fleet hetzner.com` (exact form; keep open).
2. Firefox → SOCKS5 proxy `127.0.0.1:9999`, "Proxy DNS when using SOCKS v5".
3. Verify at `ifconfig.me` (should show the server IP), then `dashboard.helius.dev`.
4. Turn the proxy back off when done.
For just the *subscription count* you can skip the tunnel:
`docker compose exec -T framework python -m scripts.helius_webhook_setup --list`.

### How we get vetted wallets (the discovery → trade pipeline)

```
Browser-Opus on office PC  →  vets Birdeye trending traders  →  KEEP/REJECT/TOO_FAST
   → OneDrive vetted_watch_results.txt
   → synced into repo bots/copy/vetted_watch_results.txt  (git push → deploy)
   → server: apply_vetting_results.py  → KEEP becomes ACTIVE
   → daily cron syncs active+watch to Helius webhooks
   → COPY trades on clusters of active wallets
```

**Vetting criteria (why a wallet is KEEP):** strongly positive **realized** PnL (banked,
not paper), unrealized losses not deeper than gains (not a bag-holder), **followable hold
time ~30 min–7 days** (faster = TOO_FAST, slower = swing/REJECT), multiple multi-x wins,
not a market-maker bot. A high rug-rate is *expected and good* — the edge is riding
fresh-mint pumps and exiting before they collapse. ~90% of candidates are REJECTed.

---

## 3.1 Why It Is The Way It Is

_Last reviewed: 2026-06-24_

The load-bearing decisions behind the design. A maintainer should understand these before
changing anything — several look like "inefficiencies" but are deliberate. Full history is
in `memory/project_decision_log.md` and `memory/project_fleet_design_state.md`.

- **STRUCTURE is paused, on purpose.** It was steadily losing paper money. The suspected
  fix needs a ~$500/month real-time data-feed upgrade, which isn't worth paying for during
  the paper phase. So effort is concentrated on COPY. STRUCTURE's code is intact and can be
  revived later if/when the data spend is justified — don't delete it.

- **Paper-first, with a kill-criteria window.** The entire point is to *measure* whether an
  edge exists before risking money. Hence locked config during the window, and a Sharpe/win-rate
  scorecard. Don't short-circuit it by going live early or tuning mid-window.

- **kill-criteria alerts a human; it does not auto-halt for strategy.** Deliberate — to
  keep human judgment in the loop and avoid bots reacting to bots. (Severe drawdown limits
  *do* auto-halt; that's different.)

- **COPY's edge is "ride the pump, exit before the rug."** This is why a high rug-rate on a
  wallet is *good*, why vetting rewards **realized** PnL and short holds, and why fast
  mechanical exits (tiered ladder, sell-cluster, rug check) exist. Don't "fix" the bot to
  avoid rug-prone tokens — that's the opposite of the strategy.

- **Vetted-only, quality-over-quantity wallet pool.** Auto-discovery is ~90% noise, so
  wallets are vetted before they can trade, and the raw discovery crons are **paused**.
  Vetted KEEP wallets go **straight to active** (vetted wallets should be *used*, not parked
  cold). The active target is **300**, grown deliberately and watched against the Helius
  credit budget.

- **The "too fast / too slow" wallet brackets.** COPY can only follow wallets it can
  realistically execute alongside: ~30 min–7 day holds. Faster snipers (seconds) are
  flagged **TOO_FAST** (logged for a possible future MEV-grade bot, not used). Slow swing
  whales are parked as a separate "SWING-COPY" idea. A wallet being *profitable for itself*
  is not the same as *profitable for COPY to follow* — that distinction drove unpinning the
  ultra-fast wallets.

- **Wallet demotion is by attributed PnL, not raw activity.** The best wallet does ~1,500
  swaps/day; an activity filter would wrongly kill it. So losers are culled by their COPY
  attributed PnL, with "one rug ≠ a loser" protection (judge excluding the single worst
  trade).

- **Helius cost is webhook deliveries (~99.7%).** Cost scales with subscribed wallets ×
  their activity; MM/HFT bots are the expensive ones (and bad signal), so the vetting that
  rejects them is also the budget control. Both active and watch tiers are subscribed.

- **Browser discovery is attended, not headless.** Headless `claude -p` has no
  Claude-in-Chrome tools, so a scheduled fully-automated browser run is impossible with this
  toolchain. The truly-unattended path would be a paid Birdeye API tier (declined on cost).
  So discovery is a person-initiated task.

- **Auto-pull deploy + commit-don't-scp.** `git push` deploys in ~60s; `reset --hard` wipes
  untracked files, so anything the bot needs is committed, not copied onto the server.

- **No secrets in git.** All keys live in the server `.env` (gitignored) and on the offline
  Access Sheet. This is why disaster recovery hinges on the offline copy.

---

## 3.2 Troubleshooting

_Last reviewed: 2026-06-24_

Symptom → likely cause → fix. Start with the symptom you see in Discord/logs.

### "A discovery pass added 0 rows" / Discord shows ⚠ 0-rows
- **Cause:** the browser session wasn't actually driving Birdeye — usually the Chrome
  extension's service worker went idle, or a Cloudflare/login wall appeared.
- **Fix:** re-open/reconnect Chrome (and `/chrome` if using the CLI), confirm Birdeye is
  logged in, re-run. The file-as-checkpoint means completed batches were already saved.

### "claude is not recognized" (in a scheduled/script run)
- **Cause:** the Claude Code CLI isn't on that process's PATH (Task Scheduler launches with
  a stale env), or it isn't installed as a standalone CLI.
- **Fix:** install/locate the CLI (`(Get-Command claude).Source`) and hardcode the path; but
  note the headless browser path doesn't work anyway — run discovery **attended**.

### "Heartbeat silent" / a service went quiet (P1)
- **Cause:** a container crashed or wedged.
- **Fix:** `docker compose ps`; `docker compose logs --tail=100 <svc>`;
  `docker compose up -d <svc>` (or `--force-recreate`). The watchdog also auto-tries a
  restart; a P0 follows if that fails.

### "kill criterion fired" (P1)
- **Cause:** a strategy metric (win rate, consecutive losses, Sharpe) crossed a threshold.
- **Fix:** **none required automatically** — it's informational. Note it; a human decides
  later whether to continue/halt/extend. Do **not** start tuning config in response (resets
  the window).

### "Helius credits spiking" / approaching the 30M cap
- **Cause:** too many or too-active subscribed wallets (often an MM/HFT wallet slipped in).
- **Fix:** check `credit_pool_snapshot` (top heavy-hitters) and the Helius dashboard; prune
  the offender, slow discovery, or raise the budget. Remember both active + watch cost
  credits.

### "Helius webhook 400 / sync errors"
- **Cause:** address-list churn or a malformed sync.
- **Fix:** `docker compose exec -T framework python -m scripts.helius_webhook_setup --list`
  to inspect; re-run the daily sync; check `HELIUS_*` env vars are set.

### "A trade booked a fake profit on a rugged token"
- **Cause:** a pre-fix orphan, or a stale price at close.
- **Fix:** the rug check now prevents new cases; for a historical one, use
  `scripts/correct_rug_trade.py` (targeted, audited) — do **not** bulk-edit trades.

### "I changed `.env` but nothing happened"
- **Cause:** `restart` doesn't re-read `.env`.
- **Fix:** `docker compose up -d --force-recreate <svc>`; verify `docker compose exec <svc> printenv VAR`.

### "A deploy broke something"
- **Fix:** `git revert <commit> && git push` — auto-pull rolls it forward in ~60s. Or
  `touch ~/crypto-fleet/.autopull_paused` to freeze while you investigate.

### "Helius dashboard won't load"
- **Cause:** not tunneling through the server (account is German-IP bound), or the proxy is
  still on after you finished.
- **Fix:** use `ssh -D 9999 -N fleet hetzner.com` + Firefox SOCKS5 `127.0.0.1:9999`; verify
  via `ifconfig.me`; turn the proxy off afterward.

### Known non-obvious gotchas (catalog)
The full, evolving list of subtle pitfalls lives in `memory/project_ops_gotchas.md`
(e.g. container runs *baked* code so data files need `docker compose cp`; auto-pull wipes
untracked files; `restart` ignores `.env`; PowerShell 5.1 mangles non-ASCII in `.ps1`).
When something weird happens, check there.

---

## 3.3 Access Sheet (PRINT THIS — fill by hand)

_Last reviewed: 2026-06-24_

> **This page is a blank template.** It contains **no passwords** and never should — it is
> in git. **Print it, then write the credentials in by hand** and store it with your family
> password sheets, somewhere safe and offline. Whoever maintains the project needs this
> sheet plus the printed `.env` (see bottom).
>
> **Print in LANDSCAPE** so there's room to write. The build produces a ready-to-print
> landscape version of just this sheet at **`docs/ACCESS_SHEET.html`** (open in a browser →
> Print → Landscape). The full manual's `docs/MANUAL.html` is portrait.

### Accounts & consoles

| # | Service | What it unlocks | Console / login URL | Account / email | Username | Password | 2FA? | Notes |
|---|---|---|---|---|---|---|---|---|
| 1 | **Hetzner** | The server + backups | console.hetzner.cloud | __________ | __________ | __________ | ☐ | also need SSH key for the box |
| 2 | **SSH key (server)** | Logging into the server | (key file on the PC) | n/a | `fleet` | (key passphrase) ____ | ☐ | connect: `ssh fleet hetzner.com` |
| 3 | **GitHub** | Code; push = deploy | github.com | __________ | __________ | __________ | ☐ | repo: RoyTK/crypto-trading-fleet |
| 4 | **Helius** | Solana webhooks (COPY) | dashboard.helius.dev | __________ | __________ | __________ | ☐ | **open via SSH SOCKS tunnel** (see Tools) |
| 5 | **Cielo** | Wallet PnL stats | app.cielo.finance | __________ | __________ | __________ | ☐ | |
| 6 | **Birdeye** | Token data / discovery | birdeye.so | __________ | __________ | __________ | ☐ | keep Chrome logged in for discovery |
| 7 | **Discord** | Alerts + `/panic` | discord.com | __________ | __________ | __________ | ☐ | note the server + #alerts channel |
| 8 | **Telegram** | Backup alert channel | t.me | __________ | __________ | __________ | ☐ | |
| 9 | **Twilio** | Emergency SMS | console.twilio.com | _(not set up)_ | — | — | — | NOT configured (no SMS in paper phase) |
| 9b | **Privacy.com** | Pays for some services (Helius etc.) | privacy.com | __________ | __________ | __________ | ☐ | virtual cards |
| 10 | **Cloudflare** | Remote access tunnel | one.dash.cloudflare.com | __________ | __________ | __________ | ☐ | |
| 11 | **Microsoft / OneDrive** | Vetting result files | onedrive.com | __________ | __________ | __________ | ☐ | files: `OneDrive\Documents\Claude\` |
| 12 | **Claude (Max)** | Runs wallet discovery | claude.ai | __________ | __________ | __________ | ☐ | Max subscription |
| 13 | **Email (for reports)** | Daily report email / SMTP | __________ | __________ | __________ | __________ | ☐ | |
| 14 | **Recovery codes** | 2FA backups for the above | (store with this sheet) | | | | | one-time codes |

### Crypto wallets & recovery phrases (the MOST sensitive items)

> ⚠️ **A 12-word recovery phrase IS the money.** Anyone who has it can drain that wallet.
> Write these by hand on the printed sheet ONLY, store them in a safe/lockbox, and **never**
> type or photograph them or store them on any computer, phone, cloud, or in this project.
> If a phrase is ever exposed, move the funds to a new wallet immediately.

| Wallet | Type / chain | Used for | Address (public, ok to write) | 12-word recovery phrase (store securely) | Notes |
|---|---|---|---|---|---|
| **Phantom** (phantom.com) | Solana | COPY trading / funding | __________ | 1.____ 2.____ 3.____ … 12.____ | |
| **Rabby** (rabby.io) | EVM (Base/Arbitrum/ETH) | EVM trading / funding | __________ | 1.____ 2.____ 3.____ … 12.____ | |
| **MetaMask** | EVM | EVM trading / funding | __________ | 1.____ 2.____ 3.____ … 12.____ | |
| **Kraken** (kraken.com) | Exchange | On-ramp / hold capital | (account, not a phrase) | login on the accounts table above | enable 2FA |

### The live `.env` (the master key file)

The server's `~/crypto-fleet/.env` holds **every API key and the trading-wallet secret**. It
is intentionally **not** in git and **cannot be regenerated** if lost. To keep a physical
backup:

```bash
ssh fleet hetzner.com 'cat ~/crypto-fleet/.env'    # then print/save the output offline
```

Store the printed `.env` **with this Access Sheet**, securely. Treat it like the keys to a
safe — anyone with it can move money once live trading is on.

### Who can use the emergency stop

`/panic` only works for authorized Discord/Telegram users (by user ID). **Make sure at
least one trusted person besides Roy is configured** (`DISCORD_OWNER_USER_ID` /
`TELEGRAM_OWNER_USER_ID`). Record who: ____________________________________________

---

## 3.4 Appendix

_Last reviewed: 2026-06-24_

### Crontab inventory (server, UTC)

Verify the live set with `crontab -l`. Expected:
- `* * * * *` — `scripts/hetzner_autopull.sh` (auto-deploy)
- `0 7 * * *` — `scripts.wallet_pool_daily_cron` (pool reconcile + Helius sync)
- `30 7 * * *` — `scripts.apply_vetting_results` (ingest vetting)
- `15 13 * * *` — `scripts.credit_pool_snapshot` (credit/pool curve)
- `~13:00` — `scripts.daily_digest` (COPY 24h summary — verify installed)
- every 5h — STRUCTURE whale_pool_growth
- weekly — STRUCTURE whale_graduation_scan
- `0 14 1 2,5,8,11 *` — `scripts.quarterly_whale_refresh`
- `0 6/14/22 * * *` — 3× `scripts.wallet_pool_discovery` — **PAUSED** (commented)

### Env-var reference (names + purpose only — values live in `.env`)

| Var | Purpose |
|---|---|
| `HELIUS_API_KEY`, `HELIUS_RPC_URL`, `HELIUS_WEBHOOK_AUTH_SECRET` | Solana webhooks/RPC + receiver auth |
| `CIELO_API_KEY` | wallet PnL stats |
| `BIRDEYE_API_KEY` | token data |
| `COPY_LIVE_ENABLED`, `COPY_LIVE_FULL_ENABLED` | real-money gates (OFF) |
| `COPY_SOLANA_PRIVATE_KEY` | COPY trading wallet secret (empty until live) |
| `COPY_CLUSTER_BUY_ENABLED` | COPY buying on/off (currently ON) |
| `COPY_DISCORD_WEBHOOK` | discovery health pings |
| `HYPERLIQUID_AGENT_PRIVATE_KEY`, `HYPERLIQUID_*` | STRUCTURE venue (trade-only key) |
| `COINGLASS_API_KEY`, `STRUCTURE_LIQ_CASCADE_ENABLED` | liquidation feed (disabled) |
| `DISCORD_*`, `TELEGRAM_*` | alerting channels (active) |
| `TWILIO_*` | SMS — **not configured** (no SMS in paper phase) |
| `SMTP_*`, `SMTP_TO` | email digest/report; `SMTP_TO` = `trading@generalaisystems.com` |
| `CLOUDFLARE_TUNNEL_TOKEN` | remote access |
| drawdown/halt knobs (`*_DD_*_PCT`, `CONSECUTIVE_LOSS_*`, `FLEET_*`) | risk limits |

See `.env.example` for the full list.

### Key files

| Path | What |
|---|---|
| `docker-compose.yml` | all services |
| `scripts/hetzner_autopull.sh` | auto-deploy |
| `bots/copy/config.py`, `bots/structure/config.py` | locked thresholds |
| `bots/copy/wallet_pool_manager.py` | tier-decision logic |
| `scripts/apply_vetting_results.py` | vetting → pool |
| `scripts/credit_pool_snapshot.py` | credit/pool snapshot |
| `bots/copy/discovery_automation/` | wallet discovery prompt + (attended) wrapper |
| `framework/models.py` | DB tables |
| `framework/kill_criteria_monitor.py` | scorecard + window dates |

### DB tables (quick map)

`bot_state` (state + kill-criteria JSON), `trades`, `signals`, `wallet_pool`,
`wallet_events_log`, `wallet_attributions`, `cluster_detections`, `shadow_signals`,
`copy_signal_shadow_log`, `structure_whale_pool`, `halts`, `scores`, `audit_log`,
`heartbeats`.

### Deeper docs (read these for detail this manual summarizes)

- `OPERATIONS.md` — full ops runbook (phases, costs, deploy, backups, /panic).
- `OPS_CHEATSHEET.md` — dense quick reference (window rules, commands, services).
- `design_state_2026-04-26.md` — frozen original design snapshot (history, not current).
- `bots/copy/discovery_automation/README.md` — discovery automation specifics.
- `memory/` (Claude's notes): `project_decision_log.md`, `project_fleet_design_state.md`,
  `project_ops_gotchas.md`, `reference_helius_dashboard_access.md`,
  `reference_onedrive_vetting_files.md`, `feedback_wallet_curation_criteria.md`.

### Maintaining this manual

Edit the section files in `docs/manual/`, run `python scripts/build_manual.py`, commit the
sources + rebuilt `docs/MANUAL.md` (and `.html`). Docs changes don't restart anything. See
`docs/manual/README.md`.
